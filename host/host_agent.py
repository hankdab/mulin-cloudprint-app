"""
打印主机端代理 (Host Agent)
- 通过 WebSocket 连接云服务器
- 接收打印任务并调用本地打印机
- 提供本地 Web 配置页面
"""
import asyncio
import json
import os
import sys
import time
import logging
import tempfile
import threading
import requests
import websockets
from flask import Flask, render_template, request, jsonify

from print_engine import list_printers, get_default_printer, print_file, get_platform

# ============ 日志 ============

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger('host_agent')

# ============ 路径兼容 (PyInstaller) ============

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def get_resource_dir():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_dir()
RESOURCE_DIR = get_resource_dir()

# ============ 配置 ============

CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), 'cloudprint_downloads')
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'server_url': 'http://localhost:9000',
        'host_key': '',
        'printer_name': '',
        'auto_start': True,
        'config_port': 9100
    }

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

config = load_config()

# ============ 状态 ============

agent_state = {
    'connected': False,
    'token': None,
    'printer_info': None,
    'org_info': None,
    'jobs_processed': 0,
    'last_error': None
}

# ============ WebSocket 客户端 ============

def authenticate():
    try:
        resp = requests.post(
            f"{config['server_url']}/api/auth/host",
            json={'hostKey': config['host_key'], 'platform': get_platform()},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            agent_state['printer_info'] = data.get('printer')
            agent_state['org_info'] = data.get('org')
            logger.info(f"认证成功: 打印机={data['printer']['name']}")
            return data['token']
        else:
            logger.error(f"认证失败: {resp.json().get('error', '未知错误')}")
            return None
    except Exception as e:
        logger.error(f"认证请求失败: {e}")
        return None

_ws_instance = None
_message_queue = []

async def ws_client_v2():
    global _ws_instance
    while True:
        if not config.get('host_key'):
            logger.info('未配置主机密钥，等待配置...')
            await asyncio.sleep(5)
            config.update(load_config())
            continue

        token = authenticate()
        if not token:
            logger.error('认证失败，10秒后重试')
            await asyncio.sleep(10)
            continue

        agent_state['token'] = token
        server_url = config['server_url'].replace('http://', 'ws://').replace('https://', 'wss://')
        ws_url = f"{server_url}/ws?token={token}&type=host&platform={get_platform()}"

        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                _ws_instance = ws
                agent_state['connected'] = True
                agent_state['last_error'] = None
                logger.info('已连接到云服务器')

                while _message_queue:
                    await ws.send(_message_queue.pop(0))

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get('type') == 'new_job':
                        await process_job_with_ws(ws, msg['job'])
                    elif msg.get('type') == 'pending_jobs':
                        for job in msg.get('jobs', []):
                            await process_job_with_ws(ws, job)

        except Exception as e:
            agent_state['connected'] = False
            agent_state['last_error'] = str(e)
            _ws_instance = None
            logger.error(f'连接异常: {e}')

        logger.info('5秒后重连...')
        await asyncio.sleep(5)

async def process_job_with_ws(ws, job):
    job_id = job['id']
    filename = job['filename']
    copies = job.get('copies', 1)
    print_settings = {
        'paper_size': job.get('paper_size', 'A4'),
        'orientation': job.get('orientation', 'portrait'),
        'color_mode': job.get('color_mode', 'color'),
        'duplex': job.get('duplex', 'none'),
    }
    logger.info(f'收到打印任务: {filename} (x{copies}) 设置:{print_settings}')

    async def report(status, error=None):
        try:
            await ws.send(json.dumps({
                'type': 'job_status', 'jobId': job_id,
                'status': status, 'error': error
            }))
        except Exception:
            pass

    await report('sending')
    try:
        file_path = download_file(job_id, filename)
        if not file_path:
            await report('failed', '文件下载失败')
            return

        await report('printing')
        printer_name = config.get('printer_name') or None
        success, message = print_file(file_path, printer_name, copies, print_settings)

        if success:
            await report('completed')
            agent_state['jobs_processed'] += 1
            logger.info(f'打印完成: {filename}')
        else:
            await report('failed', message)
            logger.error(f'打印失败: {filename} - {message}')

        try:
            os.unlink(file_path)
        except Exception:
            pass
    except Exception as e:
        await report('failed', str(e))
        logger.error(f'任务异常: {e}')

def download_file(job_id, filename):
    try:
        url = f"{config['server_url']}/api/jobs/{job_id}/file"
        resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {agent_state["token"]}'},
            timeout=120, stream=True
        )
        if resp.status_code == 200:
            file_path = os.path.join(DOWNLOAD_DIR, filename)
            with open(file_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            logger.info(f'文件下载完成: {file_path}')
            return file_path
        else:
            logger.error(f'文件下载失败: HTTP {resp.status_code}')
            return None
    except Exception as e:
        logger.error(f'文件下载异常: {e}')
        return None

# ============ Flask 本地配置页 ============

flask_app = Flask(__name__, template_folder=os.path.join(RESOURCE_DIR, 'templates'))

@flask_app.route('/')
def index():
    return render_template('host_config.html')

@flask_app.route('/api/config', methods=['GET'])
def get_config():
    cfg = load_config()
    return jsonify({
        'server_url': cfg.get('server_url', ''),
        'host_key': cfg.get('host_key', ''),
        'printer_name': cfg.get('printer_name', ''),
        'auto_start': cfg.get('auto_start', True)
    })

@flask_app.route('/api/config', methods=['POST'])
def set_config():
    global config
    data = request.json
    config.update(data)
    save_config(config)
    return jsonify({'ok': True})

@flask_app.route('/api/status')
def get_status():
    return jsonify({
        'connected': agent_state['connected'],
        'printer_info': agent_state['printer_info'],
        'org_info': agent_state['org_info'],
        'jobs_processed': agent_state['jobs_processed'],
        'last_error': agent_state['last_error'],
        'platform': get_platform()
    })

@flask_app.route('/api/local-printers')
def get_local_printers():
    printers = list_printers()
    default = get_default_printer()
    return jsonify({'printers': printers, 'default': default})

@flask_app.route('/api/test-print', methods=['POST'])
def test_print():
    printer_name = config.get('printer_name') or None
    test_file = os.path.join(DOWNLOAD_DIR, 'test_print.txt')
    with open(test_file, 'w', encoding='utf-8') as f:
        f.write('云打印测试页\n')
        f.write('='*40 + '\n')
        f.write(f'时间: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
        f.write(f'平台: {get_platform()}\n')
        f.write(f'打印机: {printer_name or "默认"}\n')
        f.write('='*40 + '\n')
        f.write('如果您能看到此页面，说明打印机配置正确！\n')
    success, message = print_file(test_file, printer_name, 1)
    try:
        os.unlink(test_file)
    except Exception:
        pass
    return jsonify({'success': success, 'message': message})

# ============ 启动 ============

def run_flask():
    flask_app.run(host='127.0.0.1', port=config.get('config_port', 9100), debug=False)

def main():
    logger.info('云打印主机端启动中...')
    logger.info(f'配置页面: http://localhost:{config.get("config_port", 9100)}')
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    asyncio.run(ws_client_v2())

if __name__ == '__main__':
    main()
