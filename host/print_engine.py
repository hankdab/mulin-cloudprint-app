"""
打印引擎 - 跨平台打印支持
Windows: win32print API
Linux/UOS: CUPS API (via lp/lpr 命令)
"""
import os
import sys
import subprocess
import tempfile
import logging

logger = logging.getLogger('print_engine')

def get_platform():
    if sys.platform == 'win32':
        return 'windows'
    elif sys.platform == 'darwin':
        return 'macos'
    else:
        return 'linux'

def list_printers():
    platform = get_platform()
    if platform == 'windows':
        return _list_printers_win()
    else:
        return _list_printers_cups()

def get_default_printer():
    platform = get_platform()
    if platform == 'windows':
        return _get_default_printer_win()
    else:
        return _get_default_printer_cups()

def print_file(file_path, printer_name=None, copies=1, print_settings=None):
    if not os.path.exists(file_path):
        return False, f'文件不存在: {file_path}'
    settings = print_settings or {}
    platform = get_platform()
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in ('.pdf', '.ps', '.pcl', '.txt', '.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff'):
        converted = _convert_to_pdf(file_path)
        if converted:
            file_path = converted
        else:
            logger.warning(f'无法转换文件格式 {ext}，尝试直接打印')
    logger.info(f'打印设置: 纸张={settings.get("paper_size","A4")}, '
                f'方向={settings.get("orientation","portrait")}, '
                f'色彩={settings.get("color_mode","color")}, '
                f'双面={settings.get("duplex","none")}')
    try:
        if platform == 'windows':
            return _print_file_win(file_path, printer_name, copies, settings)
        else:
            return _print_file_cups(file_path, printer_name, copies, settings)
    except Exception as e:
        return False, str(e)

def _convert_to_pdf(file_path):
    try:
        output_dir = tempfile.mkdtemp(prefix='cloudprint_')
        cmd = ['libreoffice', '--headless', '--convert-to', 'pdf', '--outdir', output_dir, file_path]
        if get_platform() == 'windows':
            lo_paths = [
                r'C:\Program Files\LibreOffice\program\soffice.exe',
                r'C:\Program Files (x86)\LibreOffice\program\soffice.exe',
            ]
            lo_bin = None
            for p in lo_paths:
                if os.path.exists(p):
                    lo_bin = p
                    break
            if lo_bin:
                cmd[0] = lo_bin
        result = subprocess.run(cmd, capture_output=True, timeout=60, text=True)
        if result.returncode == 0:
            basename = os.path.splitext(os.path.basename(file_path))[0]
            pdf_path = os.path.join(output_dir, basename + '.pdf')
            if os.path.exists(pdf_path):
                logger.info(f'文件转换成功: {file_path} -> {pdf_path}')
                return pdf_path
        logger.warning(f'LibreOffice 转换失败: {result.stderr}')
        return None
    except FileNotFoundError:
        logger.warning('LibreOffice 未安装，无法转换 Office 文档')
        return None
    except Exception as e:
        logger.warning(f'文件转换异常: {e}')
        return None

# ============ Windows 实现 ============

def _list_printers_win():
    try:
        import win32print
        printers = []
        for flags, desc, name, comment in win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        ):
            printers.append(name)
        return printers
    except ImportError:
        logger.error('win32print 未安装，请运行: pip install pywin32')
        return []

def _get_default_printer_win():
    try:
        import win32print
        return win32print.GetDefaultPrinter()
    except ImportError:
        return None

def _print_file_win(file_path, printer_name, copies, settings=None):
    ext = os.path.splitext(file_path)[1].lower()
    settings = settings or {}

    def _build_sumatra_settings():
        parts = [f'{copies}x']
        orientation = settings.get('orientation', 'portrait')
        if orientation == 'landscape':
            parts.append('landscape')
        color_mode = settings.get('color_mode', 'color')
        if color_mode == 'grayscale':
            parts.append('monochrome')
        duplex = settings.get('duplex', 'none')
        if duplex == 'long-edge':
            parts.append('duplex')
        elif duplex == 'short-edge':
            parts.append('duplexshort')
        paper = settings.get('paper_size', 'A4')
        if paper:
            parts.append(f'paper={paper}')
        return ','.join(parts)

    if ext == '.pdf':
        sumatra_paths = [
            r'C:\Program Files\SumatraPDF\SumatraPDF.exe',
            r'C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe',
        ]
        for sp in sumatra_paths:
            if os.path.exists(sp):
                print_settings_str = _build_sumatra_settings()
                cmd = [sp, '-print-to', printer_name or 'default',
                       '-print-settings', print_settings_str, '-silent', file_path]
                logger.info(f'SumatraPDF 命令: {" ".join(cmd)}')
                subprocess.run(cmd, timeout=120)
                return True, '打印任务已提交 (SumatraPDF)'

    try:
        import win32api
        import win32print
        if printer_name:
            win32print.SetDefaultPrinter(printer_name)
        for _ in range(copies):
            win32api.ShellExecute(0, 'print', file_path, None, '.', 0)
        return True, '打印任务已提交'
    except Exception as e:
        logger.warning(f'ShellExecute 打印失败: {e}')

    if ext == '.txt':
        try:
            import win32print
            hprinter = win32print.OpenPrinter(printer_name or win32print.GetDefaultPrinter())
            try:
                hjob = win32print.StartDocPrinter(hprinter, 1, ('CloudPrint', None, 'RAW'))
                win32print.StartPagePrinter(hprinter)
                with open(file_path, 'rb') as f:
                    for _ in range(copies):
                        f.seek(0)
                        win32print.WritePrinter(hprinter, f.read())
                win32print.EndPagePrinter(hprinter)
                win32print.EndDocPrinter(hprinter)
                return True, '打印完成'
            finally:
                win32print.ClosePrinter(hprinter)
        except Exception as e:
            return False, f'打印失败: {e}'

    return False, '无法找到合适的打印方式'

# ============ Linux/UOS (CUPS) 实现 ============

def _list_printers_cups():
    try:
        result = subprocess.run(['lpstat', '-p'], capture_output=True, text=True, timeout=10)
        printers = []
        for line in result.stdout.strip().split('\n'):
            if line.startswith('printer '):
                name = line.split()[1]
                printers.append(name)
        return printers
    except Exception:
        return []

def _get_default_printer_cups():
    try:
        result = subprocess.run(['lpstat', '-d'], capture_output=True, text=True, timeout=10)
        if ':' in result.stdout:
            return result.stdout.split(':')[1].strip()
        return None
    except Exception:
        return None

def _print_file_cups(file_path, printer_name, copies, settings=None):
    settings = settings or {}
    cmd = ['lp']
    if printer_name:
        cmd.extend(['-d', printer_name])
    cmd.extend(['-n', str(copies)])
    options = []
    paper_size = settings.get('paper_size', 'A4')
    if paper_size:
        options.append(f'media={paper_size}')
    orientation = settings.get('orientation', 'portrait')
    if orientation == 'landscape':
        options.append('landscape')
    orient_map = {'portrait': '3', 'landscape': '4'}
    options.append(f'orientation-requested={orient_map.get(orientation, "3")}')
    color_mode = settings.get('color_mode', 'color')
    if color_mode == 'grayscale':
        options.append('ColorModel=Gray')
    duplex = settings.get('duplex', 'none')
    duplex_map = {'none': 'None', 'long-edge': 'DuplexNoTumble', 'short-edge': 'DuplexTumble'}
    options.append(f'sides={duplex_map.get(duplex, "one-sided")}')
    for opt in options:
        cmd.extend(['-o', opt])
    cmd.append(file_path)
    logger.info(f'CUPS 打印命令: {" ".join(cmd)}')
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            return True, f'打印任务已提交: {result.stdout.strip()}'
        else:
            return False, f'打印失败: {result.stderr.strip()}'
    except Exception as e:
        return False, str(e)
