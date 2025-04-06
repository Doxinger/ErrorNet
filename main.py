import asyncio
import os
import json
import subprocess
import logging
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, Set
import sqlite3
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory, render_template
from dht_node import DHTNode

# Константы
BASE_DIR = Path(__file__).parent
FILES_DIR = BASE_DIR / 'files'
FILES_DIR.mkdir(exist_ok=True)
HTTP_PORT = 8000
DHT_PORT = 8470
DATABASE_PATH = BASE_DIR / 'local_db.sqlite'

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(BASE_DIR / 'app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Инициализация Flask
app = Flask(__name__)
dht_node = None


# Инициализация базы данных
def init_database():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            file_hash TEXT NOT NULL UNIQUE,
            file_mtime REAL NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            torrent_hash TEXT UNIQUE,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER,
            change_type TEXT NOT NULL,
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(file_id) REFERENCES files(id)
        )
    ''')
    # Проверка наличия столбца torrent_hash
    cursor.execute("PRAGMA table_info(files)")
    columns = [column[1] for column in cursor.fetchall()]
    if "torrent_hash" not in columns:
        cursor.execute("ALTER TABLE files ADD COLUMN torrent_hash TEXT UNIQUE")
    conn.commit()
    conn.close()


# Освобождение порта
def free_port(port):
    try:
        if os.name == 'nt':
            result = subprocess.run(
                f"netstat -ano | findstr :{port}",
                shell=True,
                capture_output=True,
                text=True
            )
            if result.stdout:
                for line in result.stdout.splitlines():
                    parts = line.split()
                    pid = parts[-1]
                    if pid.isdigit() and int(pid) > 0:
                        subprocess.run(f"taskkill /PID {pid} /F", shell=True, check=True)
        else:
            result = subprocess.run(
                f"lsof -i :{port} -t",
                shell=True,
                capture_output=True,
                text=True
            )
            if result.stdout:
                pids = result.stdout.split()
                for pid in pids:
                    if pid.isdigit():
                        subprocess.run(f"kill -9 {pid}", shell=True, check=True)
    except subprocess.CalledProcessError:
        pass
    except Exception as e:
        logger.error(f"Error freeing port {port}: {e}")


# Вычисление хэша содержимого файла
def calculate_content_hash(file_path):
    sha256 = hashlib.sha256()
    with file_path.open('rb') as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()


# Регистрация файла в базе данных
def register_file_in_db(file_info):
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM files WHERE content_hash = ?",
        (file_info["content_hash"],)
    )
    existing_file = cursor.fetchone()
    if existing_file:
        cursor.execute('''
            UPDATE files 
            SET file_name = ?, file_size = ?, file_mtime = ?, file_hash = ?, torrent_hash = ?
            WHERE content_hash = ?
        ''', (
            file_info["file_name"],
            file_info["file_size"],
            file_info["file_mtime"],
            file_info["file_hash"],
            file_info.get("torrent_hash"),
            file_info["content_hash"]
        ))
        change_type = "update"
        file_id = existing_file[0]
    else:
        cursor.execute('''
            INSERT INTO files (file_name, file_size, file_hash, file_mtime, content_hash, torrent_hash)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            file_info["file_name"],
            file_info["file_size"],
            file_info["file_hash"],
            file_info["file_mtime"],
            file_info["content_hash"],
            file_info.get("torrent_hash")
        ))
        change_type = "create"
        file_id = cursor.lastrowid
    cursor.execute('''
        INSERT INTO file_changes (file_id, change_type)
        VALUES (?, ?)
    ''', (file_id, change_type))
    conn.commit()
    conn.close()


# Маршруты Flask
@app.route('/')
def index():
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT f.file_name, f.file_size, f.registered_at, 
                   COUNT(fc.id) as change_count, f.torrent_hash
            FROM files f
            LEFT JOIN file_changes fc ON f.id = fc.file_id
            GROUP BY f.id
            ORDER BY f.registered_at DESC
        ''')
        files = cursor.fetchall()
        return render_template('index.html', files=files)
    except Exception as e:
        return render_template('error.html', error=str(e)), 500


@app.route('/api/files')
def get_files():
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT file_name, file_size, torrent_hash FROM files ORDER BY file_name")
        files = [{"name": row[0], "size": row[1], "torrent": row[2]} for row in cursor.fetchall()]
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        conn.close()


@app.route('/files/<path:filename>')
def serve_file(filename):
    try:
        file_path = FILES_DIR / filename
        if not file_path.exists():
            return handle_file_download(filename)
        if file_path.suffix.lower() == '.html':
            return send_from_directory(FILES_DIR, filename, mimetype='text/html')
        return send_from_directory(FILES_DIR, filename, as_attachment=True)
    except Exception as e:
        logger.error(f"Error serving file: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/peers')
def get_peers():
    if dht_node:
        peers = list(dht_node.peers)
        return jsonify({"success": True, "peers": peers})
    return jsonify({"success": False, "error": "DHT node not initialized"}), 500


@app.route('/api/torrents')
def get_torrents():
    if dht_node:
        torrents = [
            {
                "hash": file_hash,
                "info": torrent["info"]["name"],
                "size": torrent["info"]["length"]
            }
            for file_hash, torrent in dht_node.torrents.items()
        ]
        return jsonify({"success": True, "torrents": torrents})
    return jsonify({"success": False, "error": "DHT node not initialized"}), 500


@app.route('/connections')
def connections():
    return render_template('connections.html')


# Обработка загрузки файла
def handle_file_download(filename):
    try:
        torrent_hash = request.args.get('torrent')
        if not torrent_hash:
            return jsonify({"error": "Torrent hash required"}), 400
        metadata = asyncio.run(dht_node.get_value(torrent_hash))
        if not metadata:
            return jsonify({"error": "Torrent metadata not found"}), 404
        if asyncio.run(dht_node.download_torrent(metadata)):
            file_path = FILES_DIR / filename
            if file_path.exists():
                return send_from_directory(FILES_DIR, filename, as_attachment=True)
        return jsonify({"error": "File download failed"}), 500
    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({"error": str(e)}), 500


# Регистрация локальных файлов
async def register_local_files(dht_node):
    known_files = {}
    content_hashes = set()
    while True:
        try:
            current_files = {}
            for file_path in FILES_DIR.rglob('*'):
                if file_path.is_file() and file_path.stat().st_size > 0:
                    await process_file(file_path, current_files, dht_node)
            new_or_updated = {
                h: f for h, f in current_files.items()
                if h not in known_files or known_files[h]["file_mtime"] != f["file_mtime"]
            }
            for content_hash, file_info in new_or_updated.items():
                try:
                    await handle_file_registration(file_info, dht_node)
                    known_files[content_hash] = file_info
                except Exception as e:
                    logger.error(f"File registration failed: {e}")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Registration loop error: {e}")
            await asyncio.sleep(30)


# Обработка файла
async def process_file(file_path, current_files, dht_node):
    relative_path = file_path.relative_to(FILES_DIR)
    file_stat = file_path.stat()
    content_hash = calculate_content_hash(file_path)
    file_info = {
        "file_name": str(relative_path),
        "file_size": file_stat.st_size,
        "file_mtime": file_stat.st_mtime,
        "file_hash": hashlib.sha256(f"{relative_path}{file_stat.st_size}{file_stat.st_mtime}".encode()).hexdigest(),
        "content_hash": content_hash,
        "host": "127.0.0.1"
    }
    torrent = await dht_node.create_torrent(file_path)
    if torrent:
        file_info["torrent_hash"] = dht_node._calculate_torrent_hash(torrent)
    current_files[content_hash] = file_info


# Регистрация файла
async def handle_file_registration(file_info, dht_node):
    await dht_node.set_value(file_info["content_hash"], file_info)
    if file_info.get("torrent_hash"):
        await dht_node.set_value(file_info["torrent_hash"], file_info)
    register_file_in_db(file_info)
    logger.info(f"Registered file: {file_info['file_name']}")


# Основная функция
async def main():
    global dht_node
    init_database()
    free_port(HTTP_PORT)
    free_port(DHT_PORT)
    try:
        dht_node = DHTNode(port=DHT_PORT, files_dir=str(FILES_DIR))
        await dht_node.start()
        await dht_node.bootstrap([("127.0.0.1", DHT_PORT)])
        asyncio.create_task(register_local_files(dht_node))
        logger.info("Main services started")
    except Exception as e:
        logger.error(f"Initialization failed: {e}")
        raise


# Запуск Flask-приложения
def run_flask_app():
    if os.environ.get('FLASK_ENV') != 'development':
        logging.getLogger('werkzeug').setLevel(logging.WARNING)
    app.run(host="0.0.0.0", port=HTTP_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
        from threading import Thread
        flask_thread = Thread(target=run_flask_app, daemon=True)
        flask_thread.start()
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Graceful shutdown initiated")
    except Exception as e:
        logger.error(f"Critical failure: {e}")
    finally:
        # Завершение всех задач
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        logger.info("System shutdown complete")