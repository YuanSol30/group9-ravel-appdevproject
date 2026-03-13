from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from datetime import datetime
import os
import sqlite3
from google import genai  # UPDATED IMPORT
from dotenv import load_dotenv

load_dotenv()  # Load variables from .env file

app = Flask(__name__)
app.secret_key = 'ravel_super_secret_key'

# --- REAL AI CONFIGURATION (NEW SYNTAX) ---
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
if not GEMINI_API_KEY:
    raise RuntimeError('Missing Gemini API key. Set GEMINI_API_KEY (or GOOGLE_API_KEY).')
client = genai.Client(api_key=GEMINI_API_KEY)


def get_db_connection():
    conn = sqlite3.connect('ravel_database.db')
    conn.row_factory = sqlite3.Row
    return conn


def ensure_tables():
    """Create any tables that may not exist yet (e.g. on an older DB)."""
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS SearchHistory (
            HistoryID INTEGER PRIMARY KEY AUTOINCREMENT,
            UserID    INTEGER,
            ResultName TEXT,
            ResultType TEXT,
            ResultID  INTEGER,
            Genre     TEXT,
            ArtistName TEXT,
            SearchedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (UserID) REFERENCES User(UserID)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ListenHistory (
            HistoryID INTEGER PRIMARY KEY AUTOINCREMENT,
            UserID INTEGER, TrackID INTEGER, Genre TEXT,
            ListenedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (UserID) REFERENCES User(UserID),
            FOREIGN KEY (TrackID) REFERENCES Track(TrackID)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS Notification (
            NotificationID INTEGER PRIMARY KEY AUTOINCREMENT,
            UserID INTEGER NOT NULL,
            Message TEXT NOT NULL,
            IsRead INTEGER DEFAULT 0,
            CreatedAt DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (UserID) REFERENCES User(UserID)
        )
    ''')
    conn.commit()
    conn.close()

ensure_tables()


def _fmt_relative_time(ts):
    if not ts:
        return 'Recently'
    try:
        dt = datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S')
        delta = datetime.now() - dt
        secs = max(int(delta.total_seconds()), 0)
        if secs < 60:
            return 'Just now'
        if secs < 3600:
            return f'{secs // 60} min ago'
        if secs < 86400:
            return f'{secs // 3600} hr ago'
        if secs < 604800:
            return f'{secs // 86400} day ago' if secs < 172800 else f'{secs // 86400} days ago'
        return dt.strftime('%b %d')
    except Exception:
        return 'Recently'


def build_user_notifications(conn, user_id):
    rows = conn.execute('''
        SELECT NotificationID, Message, IsRead, CreatedAt
        FROM Notification
        WHERE UserID = ?
        ORDER BY CreatedAt DESC, NotificationID DESC
        LIMIT 5
    ''', (user_id,)).fetchall()
    return [
        {
            'id': row['NotificationID'],
            'text': row['Message'],
            'time': _fmt_relative_time(row['CreatedAt']),
            'read': bool(row['IsRead'])
        }
        for row in rows
    ]


def add_notification(conn, user_id, message, is_read=False):
    if not user_id or not message:
        return
    conn.execute(
        'INSERT INTO Notification (UserID, Message, IsRead) VALUES (?, ?, ?)',
        (user_id, message, 1 if is_read else 0)
    )


@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        role = request.form.get('role', 'Listener')
        if role not in ('Listener', 'Musician'):
            role = 'Listener'
        artist_name = request.form.get('artist_name', '').strip()

        conn = get_db_connection()
        try:
            conn.execute('INSERT INTO User (Email, Password, UserType) VALUES (?, ?, ?)', (email, password, role))
            conn.commit()
            if role == 'Musician' and artist_name:
                conn.execute(
                    'INSERT INTO Artist (Name, StreamCount, IsUnderrepresented) VALUES (?, ?, ?)',
                    (artist_name, 0, 1)
                )
                conn.commit()
            conn.close()
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            conn.close()
            flash('An account with that email already exists. Please log in instead.', 'error')
            return render_template('register.html')
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = get_db_connection()
        user = conn.execute('SELECT * FROM User WHERE Email = ? AND Password = ?', (email, password)).fetchone()
        conn.close()

        if user:
            session['user_id'] = user['UserID']
            session['user_type'] = user['UserType']
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password. Please try again.', 'error')
            return render_template('login.html')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))


@app.route('/search')
def search():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    notifications = build_user_notifications(conn, session['user_id'])
    conn.close()
    return render_template('search.html', notifications=notifications)


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if session.get('user_type') != 'Musician':
        flash('Only musicians can upload tracks.', 'error')
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM User WHERE UserID = ?', (session['user_id'],)).fetchone()

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        genre = request.form.get('genre', '').strip()
        artist_name = request.form.get('artist_name', '').strip()
        if not title or not genre or not artist_name:
            conn.close()
            flash('All fields are required.', 'error')
            return render_template('upload.html', email=user['Email'])

        artist = conn.execute('SELECT * FROM Artist WHERE Name = ?', (artist_name,)).fetchone()
        if not artist:
            conn.execute(
                'INSERT INTO Artist (Name, StreamCount, IsUnderrepresented) VALUES (?, ?, ?)',
                (artist_name, 0, 1)
            )
            conn.commit()
            artist = conn.execute('SELECT * FROM Artist WHERE Name = ?', (artist_name,)).fetchone()

        conn.execute(
            'INSERT INTO Track (ArtistID, Title, Genre, PlayCount) VALUES (?, ?, ?, ?)',
            (artist['ArtistID'], title, genre, 0)
        )
        add_notification(conn, session['user_id'], f'Your track "{title}" was uploaded to Ravel.')
        conn.commit()
        conn.close()
        flash('Track uploaded successfully!', 'success')
        return redirect(url_for('upload'))

    conn.close()
    default_artist = user['Email'].split('@')[0].capitalize()
    return render_template('upload.html', email=user['Email'], default_artist=default_artist)


@app.route('/profile')
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM User WHERE UserID = ?', (session['user_id'],)).fetchone()

    playlists_data = conn.execute(
        'SELECT * FROM Playlist WHERE UserID = ? ORDER BY DateCreated DESC',
        (session['user_id'],)
    ).fetchall()

    ai_count = conn.execute(
        'SELECT COUNT(*) as cnt FROM AI_Interaction WHERE UserID = ?',
        (session['user_id'],)
    ).fetchone()['cnt']

    recent_listens_rows = conn.execute('''
        SELECT Track.TrackID as track_id, Track.Title as title, Track.Genre as genre,
               Artist.Name as artist, ListenHistory.ListenedAt as listened_at
        FROM ListenHistory
        JOIN Track ON ListenHistory.TrackID = Track.TrackID
        JOIN Artist ON Track.ArtistID = Artist.ArtistID
        WHERE ListenHistory.UserID = ?
        ORDER BY ListenHistory.ListenedAt DESC
        LIMIT 12
    ''', (session['user_id'],)).fetchall()

    top_genres_rows = conn.execute('''
        SELECT Genre, COUNT(*) as cnt
        FROM ListenHistory
        WHERE UserID = ? AND Genre != ''
        GROUP BY Genre
        ORDER BY cnt DESC
        LIMIT 3
    ''', (session['user_id'],)).fetchall()
    top_genres = [r['Genre'] for r in top_genres_rows]

    suggestions_rows = []
    if top_genres:
        genre_placeholders = ','.join('?' * len(top_genres))
        recent_ids = [r['track_id'] for r in recent_listens_rows[:20]]
        id_placeholders = ','.join('?' * len(recent_ids)) if recent_ids else '0'
        query_params = top_genres + recent_ids + [8] if recent_ids else top_genres + [8]
        suggestions_rows = conn.execute(f'''
            SELECT Track.TrackID as track_id, Track.Title as title, Track.Genre as genre,
                   Artist.Name as artist, Artist.IsUnderrepresented as underrep,
                   Artist.StreamCount as stream_count
            FROM Track
            JOIN Artist ON Track.ArtistID = Artist.ArtistID
            WHERE Track.Genre IN ({genre_placeholders})
              AND Track.TrackID NOT IN ({id_placeholders})
            ORDER BY Artist.IsUnderrepresented DESC, Artist.StreamCount ASC, Track.PlayCount ASC
            LIMIT ?
        ''', query_params).fetchall()

    if not suggestions_rows:
        suggestions_rows = conn.execute('''
            SELECT Track.TrackID as track_id, Track.Title as title, Track.Genre as genre,
                   Artist.Name as artist, Artist.IsUnderrepresented as underrep,
                   Artist.StreamCount as stream_count
            FROM Track
            JOIN Artist ON Track.ArtistID = Artist.ArtistID
            ORDER BY Artist.IsUnderrepresented DESC, Artist.StreamCount ASC, Track.PlayCount ASC
            LIMIT 8
        ''').fetchall()

    playlists = []
    total_tracks = 0
    for p in playlists_data:
        tracks = conn.execute('''
            SELECT Track.Title, Artist.Name
            FROM Contains
            JOIN Track ON Contains.TrackID = Track.TrackID
            JOIN Artist ON Track.ArtistID = Artist.ArtistID
            WHERE Contains.PlaylistID = ?
        ''', (p['PlaylistID'],)).fetchall()
        total_tracks += len(tracks)
        playlists.append({
            'title': p['GeneratedTitle'],
            'date': p['DateCreated'][:10] if p['DateCreated'] else '',
            'track_count': len(tracks)
        })
    notifications = build_user_notifications(conn, session['user_id'])
    conn.close()

    username = user['Email'].split('@')[0].capitalize() if user else 'User'
    email = user['Email'] if user else ''
    user_type = user['UserType'] if user else 'Listener'

    return render_template('profile.html',
        username=username,
        email=email,
        playlist_count=len(playlists),
        total_tracks=total_tracks,
        ai_count=ai_count,
        playlists=playlists,
        user_type=user_type,
        recent_listens=[dict(r) for r in recent_listens_rows],
        suggestions=[dict(r) for r in suggestions_rows],
        top_genres=top_genres,
        notifications=notifications
    )


@app.route('/library')
def library():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    conn = get_db_connection()
    playlists_data = conn.execute(
        'SELECT * FROM Playlist WHERE UserID = ? ORDER BY DateCreated DESC',
        (session['user_id'],)
    ).fetchall()

    playlists = []
    for p in playlists_data:
        tracks = conn.execute('''
            SELECT Track.TrackID as TrackID, Track.Title as Title, Artist.Name as ArtistName, Track.Genre as Genre
            FROM Contains
            JOIN Track ON Contains.TrackID = Track.TrackID
            JOIN Artist ON Track.ArtistID = Artist.ArtistID
            WHERE Contains.PlaylistID = ?
        ''', (p['PlaylistID'],)).fetchall()
        playlists.append({
            'id': p['PlaylistID'],
            'title': p['GeneratedTitle'],
            'date': p['DateCreated'],
            'tracks': tracks
        })
    player_playlists = [
        {
            'id': playlist['id'],
            'title': playlist['title'],
            'tracks': [
                {
                    'id': track['TrackID'],
                    'title': track['Title'],
                    'artist': track['ArtistName'],
                    'genre': track['Genre']
                }
                for track in playlist['tracks']
            ]
        }
        for playlist in playlists
    ]
    notifications = build_user_notifications(conn, session['user_id'])
    conn.close()
    return render_template(
        'library.html',
        playlists=playlists,
        player_playlists=player_playlists,
        notifications=notifications
    )


@app.route('/api/library/create', methods=['POST'])
def library_create():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'success': False, 'error': 'Name is required'})
    conn = get_db_connection()
    conn.execute(
        'INSERT INTO Playlist (UserID, GeneratedTitle, DateCreated) VALUES (?, ?, ?)',
        (session['user_id'], name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    )
    conn.commit()
    playlist_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.close()
    return jsonify({'success': True, 'playlist_id': playlist_id, 'name': name})


@app.route('/api/library/rename', methods=['POST'])
def library_rename():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    data = request.json or {}
    playlist_id = data.get('playlist_id')
    new_name = data.get('new_name', '').strip()
    if not playlist_id or not new_name:
        return jsonify({'success': False, 'error': 'Missing fields'})
    conn = get_db_connection()
    conn.execute(
        'UPDATE Playlist SET GeneratedTitle = ? WHERE PlaylistID = ? AND UserID = ?',
        (new_name, playlist_id, session['user_id'])
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/library/delete', methods=['POST'])
def library_delete():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    data = request.json or {}
    playlist_id = data.get('playlist_id')
    if not playlist_id:
        return jsonify({'success': False, 'error': 'Missing playlist_id'})
    conn = get_db_connection()
    row = conn.execute(
        'SELECT PlaylistID FROM Playlist WHERE PlaylistID = ? AND UserID = ?',
        (playlist_id, session['user_id'])
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Not found'})
    conn.execute('DELETE FROM Contains WHERE PlaylistID = ?', (playlist_id,))
    conn.execute('DELETE FROM Playlist WHERE PlaylistID = ?', (playlist_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/library/add_track', methods=['POST'])
def library_add_track():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    data = request.json or {}
    playlist_id = data.get('playlist_id')
    track_id = data.get('track_id')
    if not playlist_id or not track_id:
        return jsonify({'success': False, 'error': 'Missing fields'})
    conn = get_db_connection()
    row = conn.execute(
        'SELECT PlaylistID FROM Playlist WHERE PlaylistID = ? AND UserID = ?',
        (playlist_id, session['user_id'])
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Not found'})
    existing = conn.execute(
        'SELECT 1 FROM Contains WHERE PlaylistID = ? AND TrackID = ?',
        (playlist_id, track_id)
    ).fetchone()
    if not existing:
        conn.execute('INSERT INTO Contains (PlaylistID, TrackID) VALUES (?, ?)', (playlist_id, track_id))
        details = conn.execute('''
            SELECT Playlist.GeneratedTitle as playlist_name, Track.Title as track_title
            FROM Playlist
            JOIN Track ON Track.TrackID = ?
            WHERE Playlist.PlaylistID = ? AND Playlist.UserID = ?
        ''', (track_id, playlist_id, session['user_id'])).fetchone()
        if details:
            add_notification(
                conn,
                session['user_id'],
                f'Added "{details["track_title"]}" to "{details["playlist_name"]}".'
            )
        conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/library/remove_track', methods=['POST'])
def library_remove_track():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'})
    data = request.json or {}
    playlist_id = data.get('playlist_id')
    track_id = data.get('track_id')
    if not playlist_id or not track_id:
        return jsonify({'success': False, 'error': 'Missing fields'})
    conn = get_db_connection()
    row = conn.execute(
        'SELECT PlaylistID FROM Playlist WHERE PlaylistID = ? AND UserID = ?',
        (playlist_id, session['user_id'])
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'error': 'Not found'})
    conn.execute('DELETE FROM Contains WHERE PlaylistID = ? AND TrackID = ?', (playlist_id, track_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/library/list')
def library_list():
    if 'user_id' not in session:
        return jsonify({'playlists': []})
    conn = get_db_connection()
    rows = conn.execute(
        'SELECT PlaylistID, GeneratedTitle FROM Playlist WHERE UserID = ? ORDER BY DateCreated DESC',
        (session['user_id'],)
    ).fetchall()
    conn.close()
    return jsonify({'playlists': [{'id': r['PlaylistID'], 'name': r['GeneratedTitle']} for r in rows]})


@app.route('/ai')
def ai_page():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db_connection()
    notifications = build_user_notifications(conn, session['user_id'])
    conn.close()
    return render_template('ai.html', notifications=notifications)


def _serialize_search_track(row):
    return {
        'id': row['TrackID'],
        'name': row['Title'],
        'type': 'Track',
        'genre': row['Genre'],
        'artist': row['ArtistName']
    }


def _build_search_recommendations(conn, query_text, matched_tracks, limit=5):
    lowered_query = (query_text or '').strip().lower()
    matched_ids = [row['TrackID'] for row in matched_tracks]
    matched_genres = []
    for row in matched_tracks:
        genre = (row['Genre'] or '').strip()
        if genre and genre not in matched_genres:
            matched_genres.append(genre)

    recommendation_rows = []
    seen_ids = set(matched_ids)

    if matched_genres:
        genre_placeholders = ','.join('?' * len(matched_genres))
        id_placeholders = ','.join('?' * len(matched_ids)) if matched_ids else '0'
        recommendation_rows.extend(conn.execute(f'''
            SELECT Track.TrackID, Track.Title, Track.Genre, Artist.Name as ArtistName
            FROM Track
            JOIN Artist ON Track.ArtistID = Artist.ArtistID
            WHERE Track.Genre IN ({genre_placeholders})
              AND Track.TrackID NOT IN ({id_placeholders})
            ORDER BY Artist.IsUnderrepresented DESC, Artist.StreamCount ASC, Track.PlayCount ASC
            LIMIT ?
        ''', matched_genres + matched_ids + [limit]).fetchall())

    if len(recommendation_rows) < limit:
        remaining = limit - len(recommendation_rows)
        name_like = '%' + query_text + '%'
        extra_rows = conn.execute('''
            SELECT Track.TrackID, Track.Title, Track.Genre, Artist.Name as ArtistName
            FROM Track
            JOIN Artist ON Track.ArtistID = Artist.ArtistID
            WHERE (LOWER(Track.Title) LIKE LOWER(?)
                OR LOWER(Track.Genre) LIKE LOWER(?)
                OR LOWER(Artist.Name) LIKE LOWER(?))
            ORDER BY Artist.IsUnderrepresented DESC, Artist.StreamCount ASC, Track.PlayCount ASC
            LIMIT 20
        ''', (name_like, name_like, name_like)).fetchall()
        for row in extra_rows:
            if row['TrackID'] in seen_ids:
                continue
            recommendation_rows.append(row)
            seen_ids.add(row['TrackID'])
            if len(recommendation_rows) >= limit:
                break

    if len(recommendation_rows) < limit and lowered_query:
        fallback_rows = conn.execute('''
            SELECT Track.TrackID, Track.Title, Track.Genre, Artist.Name as ArtistName
            FROM Track
            JOIN Artist ON Track.ArtistID = Artist.ArtistID
            ORDER BY Artist.IsUnderrepresented DESC, Artist.StreamCount ASC, Track.PlayCount ASC
            LIMIT 20
        ''').fetchall()
        for row in fallback_rows:
            haystack = ' '.join([
                (row['Title'] or '').lower(),
                (row['ArtistName'] or '').lower(),
                (row['Genre'] or '').lower()
            ])
            if row['TrackID'] in seen_ids:
                continue
            if lowered_query not in haystack and not any(token in haystack for token in lowered_query.split() if token):
                continue
            recommendation_rows.append(row)
            seen_ids.add(row['TrackID'])
            if len(recommendation_rows) >= limit:
                break

    return [_serialize_search_track(row) for row in recommendation_rows[:limit]]


@app.route('/api/search')
def api_search():
    if 'user_id' not in session:
        return jsonify({'results': [], 'recommendations': []})
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({'results': [], 'recommendations': []})
    conn = get_db_connection()
    artists = conn.execute(
        "SELECT ArtistID, Name FROM Artist WHERE Name LIKE ? LIMIT 8",
        ('%' + q + '%',)
    ).fetchall()
    tracks = conn.execute(
        "SELECT Track.TrackID, Track.Title, Track.Genre, Artist.Name as ArtistName "
        "FROM Track JOIN Artist ON Track.ArtistID = Artist.ArtistID "
        "WHERE Track.Title LIKE ? OR Track.Genre LIKE ? OR Artist.Name LIKE ? "
        "ORDER BY CASE "
        "WHEN Track.Title LIKE ? THEN 0 "
        "WHEN Artist.Name LIKE ? THEN 1 "
        "ELSE 2 END, Artist.IsUnderrepresented DESC, Artist.StreamCount ASC, Track.PlayCount ASC "
        "LIMIT 12",
        ('%' + q + '%', '%' + q + '%', '%' + q + '%', '%' + q + '%', '%' + q + '%')
    ).fetchall()
    recommendations = _build_search_recommendations(conn, q, tracks)
    conn.close()
    results = [{'id': r['ArtistID'], 'name': r['Name'], 'type': 'Artist',
                'genre': '', 'artist': r['Name']} for r in artists]
    results += [_serialize_search_track(r) for r in tracks]
    return jsonify({'results': results, 'recommendations': recommendations})


@app.route('/api/search_history', methods=['GET'])
def get_search_history():
    if 'user_id' not in session:
        return jsonify({'history': []})
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT ResultName, ResultType, ResultID, Genre, ArtistName,
               MAX(SearchedAt) as SearchedAt
        FROM SearchHistory WHERE UserID = ?
        GROUP BY ResultName, ResultType
        ORDER BY SearchedAt DESC LIMIT 15
    ''', (session['user_id'],)).fetchall()
    conn.close()
    return jsonify({'history': [dict(r) for r in rows]})


@app.route('/api/search_history', methods=['POST'])
def save_search_history():
    if 'user_id' not in session:
        return jsonify({'success': False})
    data = request.json or {}
    conn = get_db_connection()
    conn.execute(
        'INSERT INTO SearchHistory (UserID, ResultName, ResultType, ResultID, Genre, ArtistName) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (session['user_id'], data.get('name', ''), data.get('type', ''),
         data.get('id', 0), data.get('genre', ''), data.get('artist', ''))
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/search_history/clear', methods=['POST'])
def clear_search_history():
    if 'user_id' not in session:
        return jsonify({'success': False})
    conn = get_db_connection()
    conn.execute('DELETE FROM SearchHistory WHERE UserID = ?', (session['user_id'],))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/search_history/remove', methods=['POST'])
def remove_search_history():
    if 'user_id' not in session:
        return jsonify({'success': False})
    data = request.json or {}
    conn = get_db_connection()
    conn.execute(
        'DELETE FROM SearchHistory WHERE UserID = ? AND ResultName = ? AND ResultType = ?',
        (session['user_id'], data.get('name', ''), data.get('type', ''))
    )
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/notifications/clear', methods=['POST'])
def clear_notifications():
    if 'user_id' not in session:
        return jsonify({'success': False})
    conn = get_db_connection()
    conn.execute('DELETE FROM Notification WHERE UserID = ?', (session['user_id'],))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    conn = get_db_connection()
    playlists_data = conn.execute('SELECT * FROM Playlist WHERE UserID = ? ORDER BY DateCreated DESC',
                                  (user_id,)).fetchall()

    saved_playlists = []
    player_saved_playlists = []
    for p in playlists_data:
        tracks = conn.execute('''
            SELECT Track.TrackID, Track.Title, Artist.Name as ArtistName, Track.Genre
            FROM Contains 
            JOIN Track ON Contains.TrackID = Track.TrackID 
            JOIN Artist ON Track.ArtistID = Artist.ArtistID 
            WHERE Contains.PlaylistID = ?
        ''', (p['PlaylistID'],)).fetchall()
        saved_playlists.append({
            "id": p['PlaylistID'],
            "title": p['GeneratedTitle'],
            "date": p['DateCreated'],
            "tracks": tracks
        })
        player_saved_playlists.append({
            "id": p['PlaylistID'],
            "title": p['GeneratedTitle'],
            "tracks": [
                {
                    "id": track['TrackID'],
                    "title": track['Title'],
                    "artist": track['ArtistName'],
                    "genre": track['Genre']
                }
                for track in tracks
            ]
        })

    # Get top genres from listen history
    top_genres_rows = conn.execute('''
        SELECT Genre, COUNT(*) as cnt FROM ListenHistory
        WHERE UserID = ? AND Genre != ''
        GROUP BY Genre ORDER BY cnt DESC LIMIT 3
    ''', (user_id,)).fetchall()
    top_genre_names = [g['Genre'] for g in top_genres_rows]

    # Recommendations: underrepresented artists in user's top genres first
    recommendations = []
    if top_genre_names:
        placeholders = ','.join('?' * len(top_genre_names))
        recommendations = conn.execute(f'''
            SELECT Track.TrackID, Track.Title, Artist.Name as ArtistName, Track.Genre
            FROM Track JOIN Artist ON Track.ArtistID = Artist.ArtistID
            WHERE Artist.IsUnderrepresented = 1 AND Track.Genre IN ({placeholders})
            ORDER BY Artist.StreamCount ASC LIMIT 6
        ''', top_genre_names).fetchall()

    if not recommendations:
        recommendations = conn.execute('''
            SELECT Track.TrackID, Track.Title, Artist.Name as ArtistName, Track.Genre
            FROM Track JOIN Artist ON Track.ArtistID = Artist.ArtistID
            WHERE Artist.IsUnderrepresented = 1
            ORDER BY Artist.StreamCount ASC LIMIT 6
        ''').fetchall()

    notifications = build_user_notifications(conn, user_id)
    conn.close()

    recs = [{'id': r['TrackID'], 'title': r['Title'], 'artist': r['ArtistName'], 'genre': r['Genre']}
            for r in recommendations]

    return render_template('dashboard.html',
        playlists=saved_playlists,
        player_playlists=player_saved_playlists,
        recommendations=recs,
        top_genres=top_genre_names,
        notifications=notifications
    )


# --- THE ACTUAL AGENTIC AI BRAIN ---
def is_quota_exhausted_error(err_text: str) -> bool:
    t = (err_text or '').lower()
    return (
        'resource_exhausted' in t
        or 'quota exceeded' in t
        or '429' in t
        or 'rate limit' in t
    )


def infer_music_intent(user_msg: str) -> bool:
    text = (user_msg or '').lower()
    keywords = [
        'music', 'playlist', 'song', 'songs', 'vibe', 'mood', 'recommend',
        'discover', 'chill', 'focus', 'hype', 'workout', 'happy', 'sad',
        'energetic', 'relax'
    ]
    return any(k in text for k in keywords)


@app.route('/api/chat', methods=['POST'])
def chat():
    if 'user_id' not in session:
        return jsonify({"reply": "Error: Not logged in."})

    user_msg = request.json.get('message', '')
    user_id = session['user_id']

    conn = get_db_connection()
    conn.execute('INSERT INTO AI_Interaction (UserID, Message) VALUES (?, ?)', (user_id, user_msg))
    conn.commit()

    top_genres_rows = conn.execute('''
        SELECT Genre, COUNT(*) as cnt FROM ListenHistory
        WHERE UserID = ? AND Genre != ''
        GROUP BY Genre ORDER BY cnt DESC LIMIT 3
    ''', (user_id,)).fetchall()
    conn.close()

    genre_context = ', '.join([g['Genre'] for g in top_genres_rows]) if top_genres_rows else 'not yet known'

    prompt = f"""
    You are the Agentic AI Partner for 'Ravel', a music streaming app. Your goal is to democratize artist exposure.
    This user's top genres based on their listening history are: {genre_context}.
    The user just said: "{user_msg}"

    Respond conversationally and personalize your reply based on their genre preferences. If the user expresses a mood or wants music, you MUST explicitly ask them if they want to prioritize "new artists" or "low-stream count" tracks.
    If the user has clearly agreed to prioritize new/underrepresented artists, or low streams, you must include the exact text [TRIGGER_DISCOVERY] somewhere in your response. Keep your responses under 3 sentences.
    """

    try:
        # Using the incredibly fast and stable standard flash model
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt
        )
        bot_reply = response.text
        trigger_generation = "[TRIGGER_DISCOVERY]" in bot_reply
    except Exception as e:
        err_text = str(e)
        if is_quota_exhausted_error(err_text):
            bot_reply = (
                "AI is temporarily rate-limited right now. "
                "I can still build a discovery playlist using your listening history."
            )
            return jsonify({
                "reply": bot_reply,
                "trigger_generation": infer_music_intent(user_msg),
                "ai_unavailable": True
            })

        bot_reply = "AI is temporarily unavailable. Please try again in a moment."
        return jsonify({"reply": bot_reply, "trigger_generation": False, "ai_unavailable": True})

    clean_reply = bot_reply.replace("[TRIGGER_DISCOVERY]", "").strip()

    return jsonify({"reply": clean_reply, "trigger_generation": trigger_generation})


@app.route('/api/generate_playlist', methods=['GET'])
def generate_playlist():
    if 'user_id' not in session:
        return jsonify({'tracks': []})

    user_id = session['user_id']
    conn = get_db_connection()

    # Get user's top genres from their listen history
    top_genres_rows = conn.execute('''
        SELECT Genre, COUNT(*) as cnt FROM ListenHistory
        WHERE UserID = ? AND Genre != ''
        GROUP BY Genre ORDER BY cnt DESC LIMIT 3
    ''', (user_id,)).fetchall()
    top_genre_names = [g['Genre'] for g in top_genres_rows]

    tracks = []
    if top_genre_names:
        placeholders = ','.join('?' * len(top_genre_names))
        # Priority 1: underrepresented artists in user's top genres
        tracks = list(conn.execute(f'''
            SELECT Track.TrackID, Track.Title, Artist.Name, Track.Genre
            FROM Track JOIN Artist ON Track.ArtistID = Artist.ArtistID
            WHERE Artist.IsUnderrepresented = 1 AND Track.Genre IN ({placeholders})
            ORDER BY Artist.StreamCount ASC LIMIT 5
        ''', top_genre_names).fetchall())
        # Priority 2: fill remaining slots with any tracks in user's genres
        if len(tracks) < 5:
            existing_ids = [t['TrackID'] for t in tracks] or [0]
            id_placeholders = ','.join('?' * len(existing_ids))
            fill = conn.execute(f'''
                SELECT Track.TrackID, Track.Title, Artist.Name, Track.Genre
                FROM Track JOIN Artist ON Track.ArtistID = Artist.ArtistID
                WHERE Track.Genre IN ({placeholders}) AND Track.TrackID NOT IN ({id_placeholders})
                ORDER BY Artist.IsUnderrepresented DESC, Artist.StreamCount ASC LIMIT ?
            ''', top_genre_names + existing_ids + [5 - len(tracks)]).fetchall()
            tracks += list(fill)

    # Fallback: all underrepresented artists regardless of genre
    if not tracks:
        tracks = conn.execute('''
            SELECT Track.TrackID, Track.Title, Artist.Name, Track.Genre
            FROM Track JOIN Artist ON Track.ArtistID = Artist.ArtistID
            WHERE Artist.IsUnderrepresented = 1
            ORDER BY Artist.StreamCount ASC
        ''').fetchall()

    conn.close()
    track_list = [{'id': t['TrackID'], 'title': t['Title'], 'artist': t['Name'], 'genre': t['Genre']} for t in tracks]
    return jsonify({'tracks': track_list})


@app.route('/api/log_listen', methods=['POST'])
def log_listen():
    if 'user_id' not in session:
        return jsonify({'success': False})
    data = request.json or {}
    track_id = data.get('track_id')
    genre = data.get('genre', '')
    if not track_id:
        return jsonify({'success': False})
    conn = get_db_connection()
    conn.execute('INSERT INTO ListenHistory (UserID, TrackID, Genre) VALUES (?, ?, ?)',
                 (session['user_id'], track_id, genre))
    conn.commit()
    conn.close()
    return jsonify({'success': True})


@app.route('/api/save_playlist', methods=['POST'])
def save_playlist():
    if 'user_id' not in session:
        return jsonify({"success": False})

    user_id = session['user_id']
    track_ids = request.json.get('track_ids', [])
    playlist_title = request.json.get('title', 'Ravel Discovery Playlist')

    if not track_ids:
        return jsonify({"success": False})

    conn = get_db_connection()
    cursor = conn.execute('INSERT INTO Playlist (UserID, GeneratedTitle) VALUES (?, ?)',
                          (user_id, playlist_title))
    playlist_id = cursor.lastrowid

    for t_id in track_ids:
        conn.execute('INSERT INTO Contains (PlaylistID, TrackID) VALUES (?, ?)', (playlist_id, t_id))

    add_notification(conn, user_id, f'Your playlist "{playlist_title}" was saved to your Library.')

    conn.commit()
    conn.close()
    return jsonify({"success": True})


if __name__ == '__main__':
    app.run(debug=True)