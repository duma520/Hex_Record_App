import sys
import os
import sqlite3
import json
import hashlib
import warnings
from datetime import datetime

# 抑制 PySide6 sipPyTypeDict 弃用警告
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*sipPyTypeDict.*")

# 应用程序版本号
APP_VERSION = "5.5"
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
                             QSpinBox, QComboBox, QMessageBox, QFileDialog, QTabWidget,
                             QTextEdit, QSizePolicy, QGridLayout, QHeaderView,
                             QMenu, QMenuBar, QDockWidget, QListWidget, QListWidgetItem,
                             QColorDialog, QDialog, QInputDialog, QProgressDialog,
                             QCheckBox)
from PySide6.QtCore import Qt, QSize, QTimer, QSettings, QThread, Signal, QObject
from PySide6.QtGui import QIcon, QKeySequence, QColor, QTextCursor, QAction, QShortcut
from PySide6.QtPrintSupport import QPrinter, QPrintDialog
import threading


def init_database(db_path):
    """初始化数据库连接并创建表结构"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")  # 启用WAL模式以支持并发读写
    cursor = conn.cursor()

    # 创建用户表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 兼容旧数据库：为 users 表补充 password 列
    cursor.execute("PRAGMA table_info(users)")
    user_columns = [row[1] for row in cursor.fetchall()]
    if 'password' not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN password TEXT")

    # 创建配置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_configs (
            user_id INTEGER,
            rows INTEGER DEFAULT 10,
            cols INTEGER DEFAULT 16,
            theme TEXT DEFAULT 'default',
            auto_save INTEGER DEFAULT 0,
            auto_save_interval INTEGER DEFAULT 300000,
            FOREIGN KEY(user_id) REFERENCES users(id),
            PRIMARY KEY(user_id)
        )
    ''')

    # 创建标签表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            color TEXT DEFAULT '#FFFFFF',
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(user_id, name)
        )
    ''')

    # 创建笔记表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            rows INTEGER NOT NULL,
            cols INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')

    # 笔记标签关联表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS note_tags (
            note_id INTEGER,
            tag_id INTEGER,
            FOREIGN KEY(note_id) REFERENCES notes(id),
            FOREIGN KEY(tag_id) REFERENCES tags(id),
            PRIMARY KEY(note_id, tag_id)
        )
    ''')

    # 创建笔记内容表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS note_contents (
            note_id INTEGER,
            row INTEGER NOT NULL,
            col INTEGER NOT NULL,
            content TEXT,
            color TEXT DEFAULT '#FFFFFF',
            FOREIGN KEY(note_id) REFERENCES notes(id),
            PRIMARY KEY(note_id, row, col)
        )
    ''')

    # 创建历史记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')

    conn.commit()
    return conn


def hash_password(password):
    """对密码进行 SHA-256 哈希处理"""
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


# ===================== 多线程工作器 =====================

class BackgroundWorker(QThread):
    """通用后台工作线程 - 在后台执行耗时操作，避免UI卡顿"""
    finished_with_result = Signal(object)  # 成功时发出结果
    error_occurred = Signal(str)           # 错误时发出错误信息

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self._result = None
        self._error = None

    def run(self):
        try:
            self._result = self.func(*self.args, **self.kwargs)
        except Exception as e:
            self._error = str(e)
            self.error_occurred.emit(str(e))

    def get_result(self):
        return self._result

    def get_error(self):
        return self._error


class DBWorker(QThread):
    """数据库操作工作线程 - 自动管理数据库连接"""
    finished_with_result = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, db_path, operation, *args, **kwargs):
        super().__init__()
        self.db_path = db_path
        self.operation = operation
        self.args = args
        self.kwargs = kwargs
        self._result = None

    def run(self):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._result = self.operation(conn, *self.args, **self.kwargs)
            conn.commit()
        except Exception as e:
            self._result = None
            self.error_occurred.emit(str(e))
        finally:
            if conn:
                conn.close()

    def get_result(self):
        return self._result


class FileWorker(QThread):
    """文件操作工作线程 - 用于导入导出、备份恢复等文件I/O操作"""
    finished_with_result = Signal(object)
    error_occurred = Signal(str)
    progress = Signal(int)  # 进度百分比

    def __init__(self, operation, *args, **kwargs):
        super().__init__()
        self.operation = operation
        self.args = args
        self.kwargs = kwargs
        self._result = None

    def run(self):
        try:
            self._result = self.operation(*self.args, **self.kwargs)
        except Exception as e:
            self.error_occurred.emit(str(e))

    def get_result(self):
        return self._result


# ===================== 线程安全锁 =====================
_db_lock = threading.Lock()


def thread_safe_db_operation(func):
    """装饰器：确保数据库操作线程安全"""
    def wrapper(*args, **kwargs):
        with _db_lock:
            return func(*args, **kwargs)
    return wrapper


class LoginDialog(QDialog):
    """登录对话框 - 先登录再进入主界面"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = QSettings("HexNote", "HexNoteApp")
        db_path = self.settings.value("database_path", "hex_notes.db")
        self.db_conn = init_database(db_path)
        self.selected_user = None
        self.setWindowTitle("登录")
        if os.path.exists('icon.ico'):
            self.setWindowIcon(QIcon('icon.ico'))
        self.setFixedWidth(420)
        self.init_ui()
        self.load_users()

    def init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(10)

        # 标题
        title_label = QLabel("十六进制位置说明记录工具")
        title_label.setAlignment(Qt.AlignCenter)
        title_font = title_label.font()
        title_font.setPointSize(16)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)

        subtitle = QLabel("欢迎使用，请选择或创建用户")
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        layout.addSpacing(10)

        layout.addWidget(QLabel("用户名:"))
        self.user_combo = QComboBox()
        self.user_combo.setEditable(True)
        self.user_combo.setInsertPolicy(QComboBox.NoInsert)
        self.user_combo.setMinimumHeight(30)
        self.user_combo.currentTextChanged.connect(self.on_text_changed)
        layout.addWidget(self.user_combo)

        self.hint_label = QLabel("")
        self.hint_label.setStyleSheet("color: #888888;")
        layout.addWidget(self.hint_label)

        layout.addWidget(QLabel("密码:"))
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("无密码用户可留空")
        self.password_edit.setMinimumHeight(30)
        self.password_edit.returnPressed.connect(self.do_login)
        layout.addWidget(self.password_edit)

        self.show_password_check = QCheckBox("显示密码")
        self.show_password_check.toggled.connect(
            lambda checked: self.password_edit.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password))
        layout.addWidget(self.show_password_check)

        layout.addSpacing(10)

        # 按钮行
        btn_row = QHBoxLayout()
        self.delete_user_btn = QPushButton("删除用户")
        self.delete_user_btn.clicked.connect(self.delete_user)
        btn_row.addWidget(self.delete_user_btn)

        self.new_user_btn = QPushButton("新建用户")
        self.new_user_btn.clicked.connect(self.create_new_user)
        btn_row.addWidget(self.new_user_btn)
        btn_row.addStretch()

        self.exit_btn = QPushButton("退出")
        self.exit_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.exit_btn)

        self.login_btn = QPushButton("登录")
        self.login_btn.setDefault(True)
        self.login_btn.setMinimumWidth(90)
        self.login_btn.clicked.connect(self.do_login)
        btn_row.addWidget(self.login_btn)

        layout.addLayout(btn_row)
        self.setLayout(layout)

    def load_users(self):
        """加载已有用户列表"""
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT id, username FROM users ORDER BY username")
        self.users = cursor.fetchall()
        self.user_combo.blockSignals(True)
        self.user_combo.clear()
        for user_id, username in self.users:
            self.user_combo.addItem(username, user_id)
        self.user_combo.blockSignals(False)
        if self.users:
            self.user_combo.setCurrentIndex(0)
        self.on_text_changed(self.user_combo.currentText())

    def on_text_changed(self, text):
        """根据输入内容更新提示信息"""
        username = text.strip()
        if not username:
            self.hint_label.setText("请输入用户名")
            self.password_edit.setPlaceholderText("无密码用户可留空")
            self.delete_user_btn.setEnabled(False)
            self.new_user_btn.setEnabled(False)
            return
        user = self.find_user(username)
        if user:
            stored = self.get_password(user[0])
            if stored:
                self.hint_label.setText(f"用户 '{username}' 已设置密码，请输入密码登录")
                self.password_edit.setPlaceholderText("请输入密码")
            else:
                self.hint_label.setText(f"用户 '{username}' 未设置密码，直接登录即可")
                self.password_edit.setPlaceholderText("该用户无密码，可留空")
            self.delete_user_btn.setEnabled(True)
            self.new_user_btn.setEnabled(False)
        else:
            self.hint_label.setText(f"新用户 '{username}'，登录后自动创建")
            self.password_edit.setPlaceholderText("可选：为新用户设置密码")
            self.delete_user_btn.setEnabled(False)
            self.new_user_btn.setEnabled(True)

    def find_user(self, username):
        for user_id, name in self.users:
            if name == username:
                return (user_id, name)
        return None

    def get_password(self, user_id):
        """获取指定用户的密码哈希"""
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT password FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None

    def create_new_user(self):
        """新建用户（使用当前输入的用户名和可选密码）"""
        self.do_login()

    def do_login(self):
        """执行登录"""
        username = self.user_combo.currentText().strip()
        if not username:
            QMessageBox.warning(self, "警告", "请输入用户名!")
            return

        password = self.password_edit.text()
        user = self.find_user(username)
        if user:
            # 已有用户：未设置密码则直接登录，已设置密码则校验
            stored = self.get_password(user[0])
            if stored and hash_password(password) != stored:
                QMessageBox.warning(self, "登录失败", "密码错误!")
                self.password_edit.selectAll()
                self.password_edit.setFocus()
                return
            self.selected_user = user
            self.accept()
            return

        # 新用户：自动创建并登录（密码可留空）
        try:
            cursor = self.db_conn.cursor()
            pw_hash = hash_password(password) if password else None
            cursor.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, pw_hash)
            )
            self.db_conn.commit()
            self.selected_user = (cursor.lastrowid, username)
            self.accept()
        except sqlite3.IntegrityError:
            QMessageBox.warning(self, "警告", f"用户名 '{username}' 已存在!")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"创建用户失败: {str(e)}")

    def delete_user(self):
        """删除选中的用户"""
        username = self.user_combo.currentText().strip()
        user = self.find_user(username)
        if not user:
            QMessageBox.warning(self, "警告", "请选择要删除的用户!")
            return

        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除用户 '{username}' 吗?\n这将删除该用户的所有笔记和设置，且无法恢复!",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        try:
            cursor = self.db_conn.cursor()
            user_id = user[0]
            cursor.execute("DELETE FROM note_contents WHERE note_id IN (SELECT id FROM notes WHERE user_id = ?)", (user_id,))
            cursor.execute("DELETE FROM note_tags WHERE note_id IN (SELECT id FROM notes WHERE user_id = ?)", (user_id,))
            cursor.execute("DELETE FROM notes WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM tags WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM user_configs WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self.db_conn.commit()
            self.load_users()
            QMessageBox.information(self, "成功", f"用户 '{username}' 已删除!")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"删除用户失败: {str(e)}")


class HexNoteApp(QMainWindow):
    def __init__(self, initial_user=None):
        super().__init__()
        self.current_user = None
        self.db_conn = None
        self.settings = QSettings("HexNote", "HexNoteApp")
        self._active_workers = []  # 跟踪活跃的后台工作线程
        self._loading = False      # 加载状态标志
        self.init_db()
        self.init_ui()
        self.load_settings()
        # 如果已通过登录对话框登录，直接进入主界面
        if initial_user:
            self.after_login(initial_user)

    def _cleanup_workers(self):
        """清理已完成的工作线程"""
        self._active_workers = [w for w in self._active_workers if w.isRunning()]

    def _start_worker(self, worker, on_finished=None, on_error=None):
        """启动一个后台工作线程并连接信号"""
        self._cleanup_workers()

        def handle_finished():
            if on_finished and callable(on_finished):
                on_finished(worker)
            self._cleanup_workers()
            self._set_loading(False)

        def handle_error(err_msg):
            if on_error and callable(on_error):
                on_error(err_msg)
            else:
                QMessageBox.critical(self, "错误", f"操作失败: {err_msg}")
            self._set_loading(False)

        worker.finished.connect(handle_finished)
        if hasattr(worker, 'error_occurred'):
            worker.error_occurred.connect(handle_error)

        self._active_workers.append(worker)
        self._set_loading(True)
        worker.start()

    def _set_loading(self, loading):
        """设置加载状态，更新状态栏"""
        self._loading = loading
        if loading:
            self.statusBar().showMessage("处理中，请稍候...")
        else:
            self.statusBar().showMessage("就绪")
        
    def init_db(self):
        # 初始化数据库连接
        db_path = self.settings.value("database_path", "hex_notes.db")
        self.db_conn = init_database(db_path)
        
    def init_ui(self):
        # 设置窗口标题
        self.setWindowTitle('十六进制位置说明记录工具')
        
        # 尝试设置图标
        if os.path.exists('icon.ico'):
            self.setWindowIcon(QIcon('icon.ico'))
        
        # 创建菜单栏
        self.create_menus()
        
        # 主窗口部件
        self.main_widget = QTabWidget()
        self.setCentralWidget(self.main_widget)
        
        # 笔记管理标签页
        self.note_tab = QWidget()
        self.setup_note_tab()
        self.main_widget.addTab(self.note_tab, "笔记管理")
        
        # 搜索标签页
        self.search_tab = QWidget()
        self.setup_search_tab()
        self.main_widget.addTab(self.search_tab, "搜索")
        
        # 统计标签页
        self.stats_tab = QWidget()
        self.setup_stats_tab()
        self.main_widget.addTab(self.stats_tab, "统计")
        
        # 设置标签页
        self.settings_tab = QWidget()
        self.setup_settings_tab()
        self.main_widget.addTab(self.settings_tab, "设置")
        
        # 创建状态栏
        self.statusBar().showMessage("就绪")
        
        # 状态栏显示当前用户和切换按钮
        self.user_status_label = QLabel("当前用户: 未登录")
        self.statusBar().addPermanentWidget(self.user_status_label)
        self.switch_user_btn = QPushButton("切换用户")
        self.switch_user_btn.clicked.connect(self.switch_user)
        self.statusBar().addPermanentWidget(self.switch_user_btn)
        
        # 创建停靠窗口 - 历史记录
        self.history_dock = QDockWidget("历史记录", self)
        self.history_dock.setObjectName("historyDock")  # 添加这行
        self.history_list = QListWidget()
        self.history_dock.setWidget(self.history_list)
        self.addDockWidget(Qt.RightDockWidgetArea, self.history_dock)
        self.history_dock.setVisible(False)
        
        # 创建停靠窗口 - 标签管理
        self.tags_dock = QDockWidget("标签管理", self)
        self.tags_dock.setObjectName("tagsDock")  # 添加这行
        self.tags_list = QListWidget()
        self.tags_list.itemClicked.connect(self.on_tag_clicked)
        self.tags_dock.setWidget(self.tags_list)
        self.addDockWidget(Qt.RightDockWidgetArea, self.tags_dock)
        self.tags_dock.setVisible(False)
        
        # 设置窗口大小
        self.resize(1000, 700)
        
        # 设置自动保存定时器
        self.auto_save_timer = QTimer(self)
        self.auto_save_timer.timeout.connect(self.auto_save)
        self.auto_save_interval = 300000  # 5分钟
        
        # 注册快捷键
        self.setup_shortcuts()

        self.history_dock.setObjectName("historyDock")
        self.tags_dock.setObjectName("tagsDock")
        self.main_widget.setObjectName("mainTabWidget")
    
    def create_menus(self):
        menubar = self.menuBar()
        
        # 文件菜单
        file_menu = menubar.addMenu('文件')
        
        switch_user_action = QAction('切换用户', self)
        switch_user_action.triggered.connect(self.switch_user)
        file_menu.addAction(switch_user_action)
        
        new_user_action = QAction('新建用户', self)
        new_user_action.triggered.connect(self.add_user)
        file_menu.addAction(new_user_action)
        
        delete_user_action = QAction('删除当前用户', self)
        delete_user_action.triggered.connect(self.delete_current_user)
        file_menu.addAction(delete_user_action)
        
        file_menu.addSeparator()
        
        backup_action = QAction('备份数据库', self)
        backup_action.triggered.connect(self.backup_database)
        file_menu.addAction(backup_action)
        
        restore_action = QAction('恢复数据库', self)
        restore_action.triggered.connect(self.restore_database)
        file_menu.addAction(restore_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction('退出', self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # 编辑菜单
        edit_menu = menubar.addMenu('编辑')
        
        new_note_action = QAction('新建笔记', self)
        new_note_action.triggered.connect(self.create_new_note)
        edit_menu.addAction(new_note_action)
        
        save_note_action = QAction('保存笔记', self)
        save_note_action.triggered.connect(self.save_note)
        edit_menu.addAction(save_note_action)
        
        delete_note_action = QAction('删除笔记', self)
        delete_note_action.triggered.connect(self.delete_note)
        edit_menu.addAction(delete_note_action)
        
        edit_menu.addSeparator()
        
        import_action = QAction('导入笔记', self)
        import_action.triggered.connect(self.import_note)
        edit_menu.addAction(import_action)
        
        export_action = QAction('导出笔记', self)
        export_action.triggered.connect(self.export_note)
        edit_menu.addAction(export_action)
        
        edit_menu.addSeparator()
        
        print_action = QAction('打印笔记', self)
        print_action.triggered.connect(self.print_note)
        edit_menu.addAction(print_action)
        
        # 视图菜单
        view_menu = menubar.addMenu('视图')
        
        history_view_action = QAction('显示历史记录', self, checkable=True)
        history_view_action.toggled.connect(self.toggle_history_view)
        view_menu.addAction(history_view_action)
        
        tags_view_action = QAction('显示标签管理', self, checkable=True)
        tags_view_action.toggled.connect(self.toggle_tags_view)
        view_menu.addAction(tags_view_action)
        
        # 工具菜单
        tools_menu = menubar.addMenu('工具')
        
        stats_action = QAction('统计信息', self)
        stats_action.triggered.connect(lambda: self.main_widget.setCurrentWidget(self.stats_tab))
        tools_menu.addAction(stats_action)
        
        search_action = QAction('搜索笔记', self)
        search_action.triggered.connect(lambda: self.main_widget.setCurrentWidget(self.search_tab))
        tools_menu.addAction(search_action)
        
        tools_menu.addSeparator()
        
        encrypt_action = QAction('加密内容', self)
        encrypt_action.triggered.connect(self.encrypt_content)
        tools_menu.addAction(encrypt_action)
        
        decrypt_action = QAction('解密内容', self)
        decrypt_action.triggered.connect(self.decrypt_content)
        tools_menu.addAction(decrypt_action)
        
        # 帮助菜单
        help_menu = menubar.addMenu('帮助')
        
        about_action = QAction('关于', self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        
        help_action = QAction('帮助', self)
        help_action.triggered.connect(self.show_help)
        help_menu.addAction(help_action)
    
    def setup_shortcuts(self):
        # 通用快捷键
        QShortcut(QKeySequence("Ctrl+Q"), self, self.close)
        QShortcut(QKeySequence("Ctrl+W"), self, self.close)
        
        # 用户管理快捷键
        QShortcut(QKeySequence("Ctrl+N"), self, self.add_user)
        QShortcut(QKeySequence("Ctrl+L"), self, self.switch_user)
        
        # 笔记管理快捷键
        QShortcut(QKeySequence("Ctrl+Shift+N"), self, self.create_new_note)
        QShortcut(QKeySequence("Ctrl+S"), self, self.save_note)
        QShortcut(QKeySequence("Ctrl+D"), self, self.delete_note)
        QShortcut(QKeySequence("Ctrl+P"), self, self.print_note)
        
        # 导航快捷键
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self.main_widget.setCurrentIndex(0))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self.main_widget.setCurrentIndex(1))
        QShortcut(QKeySequence("Ctrl+3"), self, lambda: self.main_widget.setCurrentIndex(2))
        QShortcut(QKeySequence("Ctrl+4"), self, lambda: self.main_widget.setCurrentIndex(3))
    
    def setup_note_tab(self):
        layout = QVBoxLayout()
        
        # 笔记操作部分
        note_ops_layout = QHBoxLayout()
        
        self.new_note_btn = QPushButton("新建笔记 (Ctrl+Shift+N)")
        self.new_note_btn.clicked.connect(self.create_new_note)
        note_ops_layout.addWidget(self.new_note_btn)
        
        self.save_note_btn = QPushButton("保存笔记 (Ctrl+S)")
        self.save_note_btn.clicked.connect(self.save_note)
        note_ops_layout.addWidget(self.save_note_btn)
        
        self.load_note_btn = QPushButton("加载笔记")
        self.load_note_btn.clicked.connect(self.load_note)
        note_ops_layout.addWidget(self.load_note_btn)
        
        self.import_note_btn = QPushButton("导入笔记")
        self.import_note_btn.clicked.connect(self.import_note)
        note_ops_layout.addWidget(self.import_note_btn)
        
        self.export_note_btn = QPushButton("导出笔记")
        self.export_note_btn.clicked.connect(self.export_note)
        note_ops_layout.addWidget(self.export_note_btn)
        
        self.delete_note_btn = QPushButton("删除笔记 (Ctrl+D)")
        self.delete_note_btn.clicked.connect(self.delete_note)
        note_ops_layout.addWidget(self.delete_note_btn)
        
        self.print_note_btn = QPushButton("打印笔记 (Ctrl+P)")
        self.print_note_btn.clicked.connect(self.print_note)
        note_ops_layout.addWidget(self.print_note_btn)
        
        layout.addLayout(note_ops_layout)
        
        # 笔记标题和行列设置
        note_config_layout = QHBoxLayout()
        
        note_config_layout.addWidget(QLabel("笔记标题:"))
        self.note_title_edit = QLineEdit()
        note_config_layout.addWidget(self.note_title_edit, 1)
        
        note_config_layout.addWidget(QLabel("描述:"))
        self.note_desc_edit = QLineEdit()
        note_config_layout.addWidget(self.note_desc_edit, 1)
        
        note_config_layout.addWidget(QLabel("行数:"))
        self.rows_spin = QSpinBox()
        self.rows_spin.setMinimum(1)
        self.rows_spin.setMaximum(100)
        note_config_layout.addWidget(self.rows_spin)
        
        note_config_layout.addWidget(QLabel("列数:"))
        self.cols_spin = QSpinBox()
        self.cols_spin.setMinimum(1)
        self.cols_spin.setMaximum(100)
        note_config_layout.addWidget(self.cols_spin)
        
        self.update_table_btn = QPushButton("更新表格")
        self.update_table_btn.clicked.connect(self.update_table_size)
        note_config_layout.addWidget(self.update_table_btn)
        
        layout.addLayout(note_config_layout)
        
        # 笔记选择和标签
        note_select_layout = QHBoxLayout()
        note_select_layout.addWidget(QLabel("选择笔记:"))
        
        self.note_combo = QComboBox()
        note_select_layout.addWidget(self.note_combo, 1)
        
        self.add_tag_btn = QPushButton("添加标签")
        self.add_tag_btn.clicked.connect(self.add_tag_to_note)
        note_select_layout.addWidget(self.add_tag_btn)
        
        self.remove_tag_btn = QPushButton("移除标签")
        self.remove_tag_btn.clicked.connect(self.remove_tag_from_note)
        note_select_layout.addWidget(self.remove_tag_btn)
        
        layout.addLayout(note_select_layout)
        
        # 当前笔记标签显示
        self.note_tags_label = QLabel("标签:")
        layout.addWidget(self.note_tags_label)
        
        # 十六进制表格和说明编辑区
        table_edit_layout = QHBoxLayout()
        
        # 十六进制表格
        self.hex_table = QTableWidget()
        self.hex_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.hex_table.cellClicked.connect(self.on_cell_clicked)
        self.hex_table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        table_edit_layout.addWidget(self.hex_table, 1)
        
        # 说明编辑区
        edit_area_layout = QVBoxLayout()
        
        edit_area_layout.addWidget(QLabel("位置说明:"))
        self.note_content_edit = QTextEdit()
        edit_area_layout.addWidget(self.note_content_edit, 1)
        
        color_btn_layout = QHBoxLayout()
        
        self.color_btn = QPushButton("设置颜色")
        self.color_btn.clicked.connect(self.set_cell_color)
        color_btn_layout.addWidget(self.color_btn)
        
        self.clear_color_btn = QPushButton("清除颜色")
        self.clear_color_btn.clicked.connect(self.clear_cell_color)
        color_btn_layout.addWidget(self.clear_color_btn)
        
        edit_area_layout.addLayout(color_btn_layout)
        
        self.save_content_btn = QPushButton("保存说明 (Enter)")
        self.save_content_btn.clicked.connect(self.save_content)
        edit_area_layout.addWidget(self.save_content_btn)
        
        table_edit_layout.addLayout(edit_area_layout, 1)
        
        layout.addLayout(table_edit_layout, 1)
        
        # 状态栏
        self.status_label = QLabel()
        layout.addWidget(self.status_label)
        
        # 初始化表格
        self.current_note_id = None
        self.current_cell = None
        self.update_table_size()
        
        self.note_tab.setLayout(layout)
    
    def setup_search_tab(self):
        layout = QVBoxLayout()
        
        # 搜索条件
        search_cond_layout = QHBoxLayout()
        
        search_cond_layout.addWidget(QLabel("搜索内容:"))
        self.search_content_edit = QLineEdit()
        search_cond_layout.addWidget(self.search_content_edit, 1)
        
        search_cond_layout.addWidget(QLabel("标签:"))
        self.search_tag_combo = QComboBox()
        self.search_tag_combo.addItem("所有标签", None)
        search_cond_layout.addWidget(self.search_tag_combo, 1)
        
        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self.search_notes)
        search_cond_layout.addWidget(self.search_btn)
        
        layout.addLayout(search_cond_layout)
        
        # 搜索结果
        self.search_results_table = QTableWidget()
        self.search_results_table.setColumnCount(4)
        self.search_results_table.setHorizontalHeaderLabels(["笔记标题", "位置", "内容", "标签"])
        self.search_results_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.search_results_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.search_results_table.cellDoubleClicked.connect(self.open_search_result)
        layout.addWidget(self.search_results_table, 1)
        
        self.search_tab.setLayout(layout)
    
    def setup_stats_tab(self):
        layout = QVBoxLayout()
        
        # 统计信息显示
        self.stats_text = QTextEdit()
        self.stats_text.setReadOnly(True)
        layout.addWidget(self.stats_text, 1)
        
        # 刷新按钮
        self.refresh_stats_btn = QPushButton("刷新统计")
        self.refresh_stats_btn.clicked.connect(self.update_stats)
        layout.addWidget(self.refresh_stats_btn)
        
        self.stats_tab.setLayout(layout)
    
    def setup_settings_tab(self):
        layout = QVBoxLayout()
        
        # 主题设置
        theme_layout = QHBoxLayout()
        theme_layout.addWidget(QLabel("主题:"))
        
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["默认", "深色", "浅色", "蓝色", "绿色"])
        theme_layout.addWidget(self.theme_combo)
        
        self.apply_theme_btn = QPushButton("应用主题")
        self.apply_theme_btn.clicked.connect(self.apply_theme)
        theme_layout.addWidget(self.apply_theme_btn)
        
        layout.addLayout(theme_layout)
        
        # 自动保存设置
        auto_save_layout = QHBoxLayout()
        self.auto_save_check = QCheckBox("启用自动保存")
        auto_save_layout.addWidget(self.auto_save_check)
        
        self.auto_save_interval_combo = QComboBox()
        self.auto_save_interval_combo.addItems(["1分钟", "5分钟", "10分钟", "30分钟"])
        auto_save_layout.addWidget(self.auto_save_interval_combo)
        
        layout.addLayout(auto_save_layout)
        
        # 账号安全设置
        account_layout = QHBoxLayout()
        account_layout.addWidget(QLabel("账号安全:"))
        self.change_pw_btn = QPushButton("设置/修改密码")
        self.change_pw_btn.clicked.connect(self.change_password)
        account_layout.addWidget(self.change_pw_btn)
        layout.addLayout(account_layout)

        # 数据库设置
        db_layout = QHBoxLayout()
        db_layout.addWidget(QLabel("数据库路径:"))
        
        self.db_path_edit = QLineEdit()
        db_layout.addWidget(self.db_path_edit, 1)
        
        self.db_browse_btn = QPushButton("浏览...")
        self.db_browse_btn.clicked.connect(self.browse_db_path)
        db_layout.addWidget(self.db_browse_btn)
        
        layout.addLayout(db_layout)
        
        # 保存设置按钮
        self.save_settings_btn = QPushButton("保存设置")
        self.save_settings_btn.clicked.connect(self.save_settings)
        layout.addWidget(self.save_settings_btn)
        
        self.settings_tab.setLayout(layout)
    
    # 以下是新增的功能方法实现
    def search_notes(self):
        """搜索笔记内容（后台线程执行）"""
        if not self.current_user:
            QMessageBox.warning(self, "警告", "请先登录!")
            return
            
        search_text = self.search_content_edit.text().strip()
        tag_id = self.search_tag_combo.currentData()
        user_id = self.current_user[0]
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_search(conn):
            cursor = conn.cursor()
            query = """
                SELECT n.id, n.title, nc.row, nc.col, nc.content, GROUP_CONCAT(t.name, ', ')
                FROM note_contents nc
                JOIN notes n ON nc.note_id = n.id
                LEFT JOIN note_tags nt ON n.id = nt.note_id
                LEFT JOIN tags t ON nt.tag_id = t.id
                WHERE n.user_id = ?
            """
            params = [user_id]
            if search_text:
                query += " AND nc.content LIKE ?"
                params.append(f"%{search_text}%")
            if tag_id:
                query += " AND t.id = ?"
                params.append(tag_id)
            query += " GROUP BY n.id, nc.row, nc.col"
            cursor.execute(query, params)
            return cursor.fetchall()

        def on_done(worker):
            results = worker.get_result()
            if results is None:
                return
            self.search_results_table.setRowCount(len(results))
            for row, (note_id, title, row_num, col_num, content, tags) in enumerate(results):
                self.search_results_table.setItem(row, 0, QTableWidgetItem(title))
                self.search_results_table.setItem(row, 1, QTableWidgetItem(f"[{row_num:02X},{col_num:02X}]"))
                self.search_results_table.setItem(row, 2, QTableWidgetItem(content[:100] + "..." if len(content) > 100 else content))
                self.search_results_table.setItem(row, 3, QTableWidgetItem(tags if tags else ""))
                self.search_results_table.item(row, 0).setData(Qt.UserRole, (note_id, row_num, col_num))

        worker = DBWorker(db_path, do_search)
        self._start_worker(worker, on_finished=on_done)
    
    def open_search_result(self, row, col):
        """打开搜索结果"""
        item = self.search_results_table.item(row, 0)
        note_id, row_num, col_num = item.data(Qt.UserRole)
        
        # 切换到笔记管理标签页
        self.main_widget.setCurrentWidget(self.note_tab)
        
        # 加载笔记
        index = self.note_combo.findData(note_id)
        if index >= 0:
            self.note_combo.setCurrentIndex(index)
            self.load_note()
            
            # 滚动到指定单元格
            self.hex_table.scrollToItem(self.hex_table.item(row_num, col_num))
            self.hex_table.setCurrentCell(row_num, col_num)
            self.on_cell_clicked(row_num, col_num)
    
    def update_stats(self):
        """更新统计信息（后台线程执行）"""
        if not self.current_user:
            self.stats_text.setText("请先登录以查看统计信息")
            return

        user_id = self.current_user[0]
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_stats(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM notes WHERE user_id = ?", (user_id,))
            note_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM note_contents WHERE note_id IN (SELECT id FROM notes WHERE user_id = ?)",
                          (user_id,))
            content_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM tags WHERE user_id = ?", (user_id,))
            tag_count = cursor.fetchone()[0]
            cursor.execute(
                "SELECT action, details, timestamp FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 5",
                (user_id,))
            recent_actions = cursor.fetchall()
            cursor.execute(
                "SELECT t.name, COUNT(nt.note_id) FROM tags t LEFT JOIN note_tags nt ON t.id = nt.tag_id WHERE t.user_id = ? GROUP BY t.id ORDER BY COUNT(nt.note_id) DESC",
                (user_id,))
            tag_stats = cursor.fetchall()
            return (note_count, content_count, tag_count, recent_actions, tag_stats)

        def on_done(worker):
            data = worker.get_result()
            if data is None:
                return
            note_count, content_count, tag_count, recent_actions, tag_stats = data

            stats_text = f"""<h2>统计信息</h2>
        <p><b>笔记数量:</b> {note_count}</p>
        <p><b>位置说明数量:</b> {content_count}</p>
        <p><b>标签数量:</b> {tag_count}</p>
        <h3>最近活动</h3><ul>"""
            for action, details, timestamp in recent_actions:
                stats_text += f"<li>{timestamp}: {action} - {details}</li>"
            stats_text += "</ul><h3>标签使用统计</h3><ul>"
            for tag, count in tag_stats:
                stats_text += f"<li>{tag}: {count} 个笔记</li>"
            stats_text += "</ul>"
            self.stats_text.setHtml(stats_text)

        worker = DBWorker(db_path, do_stats)
        self._start_worker(worker, on_finished=on_done)
    
    def add_tag_to_note(self):
        """为当前笔记添加标签（后台线程执行）"""
        if not self.current_user or not self.current_note_id:
            QMessageBox.warning(self, "警告", "请先选择笔记!")
            return

        note_id = self.current_note_id
        user_id = self.current_user[0]
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_fetch(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name FROM tags WHERE user_id = ? AND id NOT IN (SELECT tag_id FROM note_tags WHERE note_id = ?)",
                (user_id, note_id)
            )
            return cursor.fetchall()

        def on_tags_fetched(worker):
            available_tags = worker.get_result()
            if not available_tags:
                QMessageBox.information(self, "信息", "没有可用的标签，请先创建标签!")
                return

            tag_names = [tag[1] for tag in available_tags]
            tag_id_map = {tag[1]: tag[0] for tag in available_tags}

            tag_name, ok = QInputDialog.getItem(self, "添加标签", "选择要添加的标签:", tag_names, 0, False)
            if not ok or not tag_name:
                return

            tag_id = tag_id_map[tag_name]

            def do_add(conn):
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO note_tags (note_id, tag_id) VALUES (?, ?)",
                    (note_id, tag_id)
                )
                cursor.execute(
                    "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                    (user_id, "添加标签", f"笔记ID: {note_id}, 标签: {tag_name}")
                )
                return True

            def on_added(worker):
                self.update_note_tags_display()
                if self.history_dock.isVisible():
                    self.load_history()
                QMessageBox.information(self, "成功", "标签添加成功!")

            add_worker = DBWorker(db_path, do_add)
            self._start_worker(add_worker, on_finished=on_added)

        fetch_worker = DBWorker(db_path, do_fetch)
        self._start_worker(fetch_worker, on_finished=on_tags_fetched)
    
    def remove_tag_from_note(self):
        """从当前笔记移除标签（后台线程执行）"""
        if not self.current_user or not self.current_note_id:
            QMessageBox.warning(self, "警告", "请先选择笔记!")
            return

        note_id = self.current_note_id
        user_id = self.current_user[0]
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_fetch(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT t.id, t.name FROM tags t JOIN note_tags nt ON t.id = nt.tag_id WHERE nt.note_id = ?",
                (note_id,)
            )
            return cursor.fetchall()

        def on_tags_fetched(worker):
            current_tags = worker.get_result()
            if not current_tags:
                QMessageBox.information(self, "信息", "该笔记没有标签!")
                return

            tag_names = [tag[1] for tag in current_tags]
            tag_id_map = {tag[1]: tag[0] for tag in current_tags}

            tag_name, ok = QInputDialog.getItem(self, "移除标签", "选择要移除的标签:", tag_names, 0, False)
            if not ok or not tag_name:
                return

            tag_id = tag_id_map[tag_name]

            def do_remove(conn):
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM note_tags WHERE note_id = ? AND tag_id = ?",
                    (note_id, tag_id)
                )
                cursor.execute(
                    "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                    (user_id, "移除标签", f"笔记ID: {note_id}, 标签: {tag_name}")
                )
                return True

            def on_removed(worker):
                self.update_note_tags_display()
                if self.history_dock.isVisible():
                    self.load_history()
                QMessageBox.information(self, "成功", "标签移除成功!")

            remove_worker = DBWorker(db_path, do_remove)
            self._start_worker(remove_worker, on_finished=on_removed)

        fetch_worker = DBWorker(db_path, do_fetch)
        self._start_worker(fetch_worker, on_finished=on_tags_fetched)
    
    def update_note_tags_display(self):
        """更新笔记标签显示（后台线程执行）"""
        if not self.current_note_id:
            self.note_tags_label.setText("标签: 无")
            return

        note_id = self.current_note_id
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_fetch(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT GROUP_CONCAT(t.name, ', ') FROM tags t JOIN note_tags nt ON t.id = nt.tag_id WHERE nt.note_id = ?",
                (note_id,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

        def on_done(worker):
            tags = worker.get_result()
            self.note_tags_label.setText(f"标签: {tags if tags else '无'}")

        worker = DBWorker(db_path, do_fetch)
        self._start_worker(worker, on_finished=on_done)
    
    def set_cell_color(self):
        """设置当前单元格颜色（后台线程执行）"""
        if not self.current_cell or not self.current_note_id:
            QMessageBox.warning(self, "警告", "请先选择单元格!")
            return
            
        color = QColorDialog.getColor()
        if color.isValid():
            row, col = self.current_cell
            hex_color = color.name()
            note_id = self.current_note_id
            db_path = self.settings.value("database_path", "hex_notes.db")
            content = self.note_content_edit.toPlainText()

            def do_set_color(conn):
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE note_contents SET color = ? WHERE note_id = ? AND row = ? AND col = ?",
                    (hex_color, note_id, row, col)
                )
                if cursor.rowcount == 0:
                    cursor.execute(
                        "INSERT INTO note_contents (note_id, row, col, content, color) VALUES (?, ?, ?, ?, ?)",
                        (note_id, row, col, content, hex_color)
                    )
                return True

            def on_done(worker):
                item = self.hex_table.item(row, col)
                if item:
                    item.setBackground(color)
                QMessageBox.information(self, "成功", "单元格颜色设置成功!")

            worker = DBWorker(db_path, do_set_color)
            self._start_worker(worker, on_finished=on_done)
    
    def clear_cell_color(self):
        """清除当前单元格颜色（后台线程执行）"""
        if not self.current_cell or not self.current_note_id:
            QMessageBox.warning(self, "警告", "请先选择单元格!")
            return
            
        row, col = self.current_cell
        note_id = self.current_note_id
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_clear(conn):
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE note_contents SET color = '#FFFFFF' WHERE note_id = ? AND row = ? AND col = ?",
                (note_id, row, col)
            )
            return True

        def on_done(worker):
            item = self.hex_table.item(row, col)
            if item:
                item.setBackground(QColor("#FFFFFF"))
            QMessageBox.information(self, "成功", "单元格颜色已清除!")

        worker = DBWorker(db_path, do_clear)
        self._start_worker(worker, on_finished=on_done)
    
    def on_cell_double_clicked(self, row, col):
        """双击单元格快速编辑"""
        self.on_cell_clicked(row, col)
        self.note_content_edit.setFocus()
    
    def print_note(self):
        """打印当前笔记"""
        if not self.current_note_id:
            QMessageBox.warning(self, "警告", "没有可打印的笔记!")
            return
            
        # 获取笔记信息
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT title, description FROM notes WHERE id = ?", (self.current_note_id,))
        note_info = cursor.fetchone()
        
        cursor.execute("""
            SELECT row, col, content 
            FROM note_contents 
            WHERE note_id = ? 
            ORDER BY row, col
        """, (self.current_note_id,))
        contents = cursor.fetchall()
        
        # 创建打印文档
        printer = QPrinter()
        print_dialog = QPrintDialog(printer, self)
        if print_dialog.exec() == QPrintDialog.Accepted:
            # 构建打印内容
            print_text = f"笔记标题: {note_info[0]}\n"
            if note_info[1]:
                print_text += f"描述: {note_info[1]}\n"
            
            print_text += "\n位置说明:\n"
            for row, col, content in contents:
                print_text += f"[{row:02X},{col:02X}]: {content}\n"
            
            # 打印
            doc = QTextEdit()
            doc.setPlainText(print_text)
            doc.print_(printer)
    
    def backup_database(self):
        """备份数据库（后台线程执行）"""
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "备份数据库", "hex_notes_backup.db",
            "SQLite Database (*.db);;All Files (*)", options=options
        )
        if not file_path:
            return

        import shutil
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_backup():
            shutil.copy2(db_path, file_path)
            return file_path

        def on_done(worker):
            QMessageBox.information(self, "成功", f"数据库已备份到 {file_path}")

        worker = FileWorker(do_backup)
        self._start_worker(worker, on_finished=on_done)
    
    def restore_database(self):
        """恢复数据库（后台线程执行）"""
        reply = QMessageBox.warning(
            self, "警告", "恢复数据库将覆盖当前数据，确定要继续吗?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.No:
            return

        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getOpenFileName(
            self, "恢复数据库", "", "SQLite Database (*.db);;All Files (*)", options=options
        )
        if not file_path:
            return

        import shutil
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_restore():
            shutil.copy2(file_path, db_path)
            return True

        def on_done(worker):
            # 重新连接数据库
            self.db_conn = init_database(db_path)
            # 重置登录状态
            self.current_user = None
            self.current_note_id = None
            for i in range(self.main_widget.count()):
                self.main_widget.setTabEnabled(i, False)
            if hasattr(self, 'user_status_label'):
                self.user_status_label.setText("当前用户: 未登录")
            QMessageBox.information(self, "成功", "数据库已从备份恢复!")

        worker = FileWorker(do_restore)
        self._start_worker(worker, on_finished=on_done)
    
    def toggle_history_view(self, visible):
        """切换历史记录视图显示"""
        self.history_dock.setVisible(visible)
        if visible and self.current_user:
            self.load_history()
    
    def toggle_tags_view(self, visible):
        """切换标签管理视图显示"""
        self.tags_dock.setVisible(visible)
        if visible and self.current_user:
            self.load_tags()
    
    def load_history(self):
        """加载历史记录（后台线程执行）"""
        if not self.current_user:
            return

        user_id = self.current_user[0]
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_load(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT action, details, timestamp FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 100",
                (user_id,)
            )
            return cursor.fetchall()

        def on_done(worker):
            history = worker.get_result()
            if history is None:
                return
            self.history_list.clear()
            for action, details, timestamp in history:
                self.history_list.addItem(f"{timestamp}: {action} - {details}")

        worker = DBWorker(db_path, do_load)
        self._start_worker(worker, on_finished=on_done)
    
    def load_tags(self):
        """加载标签列表（后台线程执行）"""
        if not self.current_user:
            return

        user_id = self.current_user[0]
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_load(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name, color FROM tags WHERE user_id = ? ORDER BY name",
                (user_id,)
            )
            return cursor.fetchall()

        def on_done(worker):
            tags = worker.get_result()
            if tags is None:
                return
            self.tags_list.clear()
            for tag_id, name, color in tags:
                item = QListWidgetItem(name)
                item.setData(Qt.UserRole, tag_id)
                item.setBackground(QColor(color))
                self.tags_list.addItem(item)

        worker = DBWorker(db_path, do_load)
        self._start_worker(worker, on_finished=on_done)
    
    def on_tag_clicked(self, item):
        """点击标签筛选笔记"""
        tag_id = item.data(Qt.UserRole)
        
        # 更新搜索标签组合框
        index = self.search_tag_combo.findData(tag_id)
        if index >= 0:
            self.search_tag_combo.setCurrentIndex(index)
        
        # 切换到搜索标签页并执行搜索
        self.main_widget.setCurrentWidget(self.search_tab)
        self.search_notes()
    
    def record_history(self, action, details):
        """记录历史操作"""
        if not self.current_user:
            return
            
        cursor = self.db_conn.cursor()
        cursor.execute(
            "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
            (self.current_user[0], action, details)
        )
        self.db_conn.commit()
        
        # 更新历史记录显示
        if self.history_dock.isVisible():
            self.load_history()
    
    def auto_save(self):
        """自动保存当前笔记"""
        if self.current_note_id:
            self.save_note(silent=True)
    
    def apply_theme(self):
        """应用选择的主题"""
        theme = self.theme_combo.currentText()
        
        # 简单的主题实现示例
        if theme == "深色":
            self.setStyleSheet("""
                QMainWindow, QWidget {
                    background-color: #333333;
                    color: #ffffff;
                }
                QTabWidget::pane {
                    border: 1px solid #444444;
                }
            """)
        elif theme == "浅色":
            self.setStyleSheet("""
                QMainWindow, QWidget {
                    background-color: #f5f5f5;
                    color: #000000;
                }
            """)
        else:  # 默认主题
            self.setStyleSheet("")  # 重置为默认样式
        
        # 记录用户偏好
        if self.current_user:
            cursor = self.db_conn.cursor()
            cursor.execute(
                "UPDATE user_configs SET theme = ? WHERE user_id = ?",
                (theme, self.current_user[0])
            )
            self.db_conn.commit()

    
    def save_settings(self, show_message=True):
        """保存所有应用程序和用户设置"""
        try:
            # 1. 保存自动保存设置
            auto_save = self.auto_save_check.isChecked()
            interval_text = self.auto_save_interval_combo.currentText()
            
            # 转换时间间隔为毫秒
            intervals = {
                "1分钟": 60000,
                "5分钟": 300000,
                "10分钟": 600000,
                "30分钟": 1800000
            }
            self.auto_save_interval = intervals.get(interval_text, 300000)
            
            # 2. 保存数据库路径设置
            db_path = self.db_path_edit.text().strip()
            if not db_path:
                db_path = "hex_notes.db"
            
            # 3. 保存主题设置
            current_theme = self.theme_combo.currentText()
            
            # 4. 保存到QSettings (应用程序级设置)
            self.settings.setValue("auto_save", auto_save)
            self.settings.setValue("auto_save_interval", self.auto_save_interval)
            self.settings.setValue("database_path", db_path)
            self.settings.setValue("theme", current_theme)
            
            # 5. 保存窗口状态和几何信息
            self.settings.setValue("window_geometry", self.saveGeometry())
            self.settings.setValue("window_state", self.saveState())
            
            # 6. 保存停靠窗口的可见状态
            self.settings.setValue("history_visible", self.history_dock.isVisible())
            self.settings.setValue("tags_visible", self.tags_dock.isVisible())
            
            # 7. 保存当前标签页索引
            self.settings.setValue("current_tab_index", self.main_widget.currentIndex())
            
            # 8. 保存用户特定的设置到数据库 (如果已登录)
            if self.current_user:
                cursor = self.db_conn.cursor()
                cursor.execute("""
                    INSERT OR REPLACE INTO user_configs 
                    (user_id, rows, cols, theme, auto_save, auto_save_interval) 
                    VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        self.current_user[0],
                        self.rows_spin.value(),
                        self.cols_spin.value(),
                        current_theme,
                        int(auto_save),
                        self.auto_save_interval
                    ))
                self.db_conn.commit()
                
                # 记录历史
                self.record_history("保存设置", "用户偏好设置已更新")
            
            # 9. 根据设置启动/停止自动保存定时器
            if auto_save:
                self.auto_save_timer.start(self.auto_save_interval)
            else:
                self.auto_save_timer.stop()
                
            # 10. 应用当前主题
            self.apply_theme()
            
            # 只在手动保存时显示提示
            if show_message:
                QMessageBox.information(self, "成功", "所有设置已保存!")
                
        except Exception as e:
            print(f"[DEBUG] 保存设置时出错: {str(e)}")
            QMessageBox.critical(self, "错误", f"保存设置时出错:\n{str(e)}")
            # 记录错误历史
            if self.current_user:
                self.record_history("设置保存错误", str(e))

    def load_settings(self):
        """加载应用程序设置"""
        # 自动保存设置
        auto_save = self.settings.value("auto_save", False, type=bool)
        self.auto_save_check.setChecked(auto_save)
        
        interval = self.settings.value("auto_save_interval", 300000, type=int)
        # 设置间隔选择框
        if interval == 60000:
            self.auto_save_interval_combo.setCurrentText("1分钟")
        elif interval == 300000:
            self.auto_save_interval_combo.setCurrentText("5分钟")
        elif interval == 600000:
            self.auto_save_interval_combo.setCurrentText("10分钟")
        elif interval == 1800000:
            self.auto_save_interval_combo.setCurrentText("30分钟")
        
        # 数据库路径
        db_path = self.settings.value("database_path", "hex_notes.db")
        self.db_path_edit.setText(db_path)
        
        # 主题设置
        theme = self.settings.value("theme", "默认")
        self.theme_combo.setCurrentText(theme)
        
        # 恢复窗口状态
        geometry = self.settings.value("window_geometry")
        if geometry:
            self.restoreGeometry(geometry)
        
        state = self.settings.value("window_state")
        if state:
            self.restoreState(state)
        
        # 如果启用了自动保存，启动定时器
        if auto_save:
            self.auto_save_timer.start(interval)

    
    def browse_db_path(self):
        """浏览数据库路径"""
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "选择数据库路径", 
            self.db_path_edit.text(), 
            "SQLite Database (*.db);;All Files (*)", 
            options=options
        )
        
        if file_path:
            self.db_path_edit.setText(file_path)
    
    def encrypt_content(self):
        """加密当前内容"""
        if not self.note_content_edit.toPlainText():
            QMessageBox.warning(self, "警告", "没有可加密的内容!")
            return
            
        password, ok = QInputDialog.getText(
            self, "加密内容", "输入加密密码:", QLineEdit.Password)
        
        if ok and password:
            try:
                # 这里实现简单的加密逻辑
                # 注意: 这只是一个示例，实际应用中应该使用更安全的加密方式
                import base64
                from hashlib import md5
                
                content = self.note_content_edit.toPlainText()
                key = md5(password.encode()).hexdigest()
                
                # 简单的异或加密
                encrypted = []
                for i, c in enumerate(content):
                    key_c = key[i % len(key)]
                    encrypted_c = chr(ord(c) ^ ord(key_c))
                    encrypted.append(encrypted_c)
                
                encrypted_str = base64.urlsafe_b64encode("".join(encrypted).encode()).decode()
                self.note_content_edit.setPlainText(f"[ENCRYPTED]{encrypted_str}")
                
                QMessageBox.information(self, "成功", "内容已加密!")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"加密失败: {str(e)}")
    
    def decrypt_content(self):
        """解密当前内容"""
        content = self.note_content_edit.toPlainText()
        if not content.startswith("[ENCRYPTED]"):
            QMessageBox.warning(self, "警告", "没有可解密的内容!")
            return
            
        password, ok = QInputDialog.getText(
            self, "解密内容", "输入解密密码:", QLineEdit.Password)
        
        if ok and password:
            try:
                # 这里实现简单的解密逻辑
                import base64
                from hashlib import md5
                
                encrypted_str = content[11:]  # 去掉 [ENCRYPTED] 前缀
                encrypted = base64.urlsafe_b64decode(encrypted_str).decode()
                key = md5(password.encode()).hexdigest()
                
                # 简单的异或解密
                decrypted = []
                for i, c in enumerate(encrypted):
                    key_c = key[i % len(key)]
                    decrypted_c = chr(ord(c) ^ ord(key_c))
                    decrypted.append(decrypted_c)
                
                self.note_content_edit.setPlainText("".join(decrypted))
                QMessageBox.information(self, "成功", "内容已解密!")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"解密失败: {str(e)}")
    
    def show_about(self):
        """显示关于对话框"""
        about_text = f"""
        <h2>十六进制位置说明记录工具</h2>
        <p>版本: {APP_VERSION}</p>
        <p>作者: Your Name</p>
        <p>描述: 用于记录十六进制位置的自定义说明</p>
        <p>功能:</p>
        <ul>
            <li>多用户管理</li>
            <li>自定义行列数</li>
            <li>笔记标签分类</li>
            <li>内容搜索</li>
            <li>数据统计</li>
            <li>导入/导出</li>
            <li>数据加密</li>
        </ul>
        """
        QMessageBox.about(self, "关于", about_text)
    
    def show_help(self):
        """显示帮助信息"""
        help_text = """
        <h2>帮助文档</h2>
        <h3>基本使用</h3>
        <p>1. 在"用户管理"标签页添加或选择用户登录</p>
        <p>2. 登录后切换到"笔记管理"标签页</p>
        <p>3. 创建新笔记或加载已有笔记</p>
        <p>4. 点击表格中的单元格，在右侧编辑说明内容并保存</p>
        
        <h3>快捷键</h3>
        <p>Ctrl+N: 新建用户</p>
        <p>Ctrl+L: 登录用户</p>
        <p>Ctrl+Shift+N: 新建笔记</p>
        <p>Ctrl+S: 保存笔记</p>
        <p>Ctrl+D: 删除笔记</p>
        <p>Ctrl+P: 打印笔记</p>
        <p>Ctrl+1-5: 切换标签页</p>
        
        <h3>高级功能</h3>
        <p>- 使用标签分类笔记</p>
        <p>- 搜索功能可以快速定位内容</p>
        <p>- 统计功能查看使用情况</p>
        <p>- 设置中可以更改主题和自动保存间隔</p>
        """
        
        help_dialog = QMessageBox(self)
        help_dialog.setWindowTitle("帮助")
        help_dialog.setTextFormat(Qt.RichText)
        help_dialog.setText(help_text)
        help_dialog.exec()
    
    # 以下是原有方法的增强实现
    def after_login(self, user):
        """用户登录成功后的界面初始化"""
        self.current_user = user
        username = user[1]
        self.setWindowTitle(f'十六进制位置说明记录工具 - {username}')
        
        # 启用所有标签页
        for i in range(self.main_widget.count()):
            self.main_widget.setTabEnabled(i, True)
        
        # 更新状态栏
        if hasattr(self, 'user_status_label'):
            self.user_status_label.setText(f"当前用户: {username}")
        self.statusBar().showMessage(f"欢迎, {username}!")

        db_path = self.settings.value("database_path", "hex_notes.db")

        # 在后台线程加载用户配置和初始化数据
        def do_init(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT rows, cols, theme, auto_save, auto_save_interval FROM user_configs WHERE user_id = ?", (user[0],))
            config = cursor.fetchone()
            cursor.execute("SELECT id, title FROM notes WHERE user_id = ? ORDER BY updated_at DESC", (user[0],))
            notes = cursor.fetchall()
            cursor.execute("SELECT id, name, color FROM tags WHERE user_id = ? ORDER BY name", (user[0],))
            tags = cursor.fetchall()
            cursor.execute("SELECT action, details, timestamp FROM history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 100", (user[0],))
            history = cursor.fetchall()
            cursor.execute(
                "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                (user[0], "用户登录", f"用户名: {username}")
            )
            return (config, notes, tags, history)

        def on_done(worker):
            data = worker.get_result()
            if data is None:
                return
            config, notes, tags_list, history = data

            if config:
                self.rows_spin.setValue(config[0])
                self.cols_spin.setValue(config[1])
                self.theme_combo.setCurrentText(config[2] if config[2] else "默认")
                self.auto_save_check.setChecked(bool(config[3]))
                self.auto_save_interval = config[4] if config[4] else 300000
                self.apply_theme()
            else:
                self.rows_spin.setValue(10)
                self.cols_spin.setValue(16)
                self.auto_save_check.setChecked(False)
                self.auto_save_interval = 300000

            # 加载笔记列表
            self.note_combo.clear()
            for note_id, title in notes:
                self.note_combo.addItem(title, note_id)
            if not notes:
                self.note_combo.addItem("无可用笔记", None)

            # 加载标签
            self.tags_list.clear()
            for tag_id, name, color in tags_list:
                item = QListWidgetItem(name)
                item.setData(Qt.UserRole, tag_id)
                item.setBackground(QColor(color))
                self.tags_list.addItem(item)

            # 加载历史
            self.history_list.clear()
            for action, details, timestamp in history:
                self.history_list.addItem(f"{timestamp}: {action} - {details}")

            # 更新搜索标签列表
            self.search_tag_combo.clear()
            self.search_tag_combo.addItem("所有标签", None)
            for tag_id, name in tags_list:
                self.search_tag_combo.addItem(name, tag_id)

            self.update_table_size()
            # 启动自动保存定时器
            if self.auto_save_check.isChecked():
                self.auto_save_timer.start(self.auto_save_interval)

        worker = DBWorker(db_path, do_init)
        self._start_worker(worker, on_finished=on_done)
    
    def switch_user(self):
        """打开登录对话框切换用户"""
        # 先同步保存当前笔记
        if self.current_note_id and self.current_user:
            try:
                cursor = self.db_conn.cursor()
                title = self.note_title_edit.text().strip()
                description = self.note_desc_edit.text().strip()
                cursor.execute(
                    "UPDATE notes SET title = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (title, description, self.current_note_id)
                )
                self.db_conn.commit()
            except Exception:
                pass
        
        dialog = LoginDialog(self)
        try:
            if dialog.exec() == QDialog.Accepted and dialog.selected_user:
                self.after_login(dialog.selected_user)
        finally:
            dialog.db_conn.close()
    
    def logout_user(self):
        """注销当前用户，重置界面状态"""
        if not self.current_user:
            return
            
        # 停止自动保存
        self.auto_save_timer.stop()
        
        # 记录历史
        self.record_history("用户注销", f"用户名: {self.current_user[1]}")
        
        # 重置状态
        self.current_user = None
        self.current_note_id = None
        self.current_cell = None
        self.setWindowTitle('十六进制位置说明记录工具')
        
        # 清空界面
        self.note_title_edit.clear()
        self.note_desc_edit.clear()
        self.note_content_edit.clear()
        self.hex_table.clearContents()
        self.note_tags_label.setText("标签: 无")
        
        # 禁用所有标签页
        for i in range(self.main_widget.count()):
            self.main_widget.setTabEnabled(i, False)
        
        # 更新状态栏
        if hasattr(self, 'user_status_label'):
            self.user_status_label.setText("当前用户: 未登录")
        self.statusBar().showMessage("已注销")
    
    def create_new_note(self):
        """创建新笔记（后台线程执行）"""
        if not self.current_user:
            QMessageBox.warning(self, "警告", "请先登录!")
            return
            
        title = self.note_title_edit.text().strip()
        if not title:
            QMessageBox.warning(self, "警告", "请输入笔记标题!")
            return
            
        description = self.note_desc_edit.text().strip()
        rows = self.rows_spin.value()
        cols = self.cols_spin.value()
        user_id = self.current_user[0]
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_create(conn):
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO notes (user_id, title, description, rows, cols) VALUES (?, ?, ?, ?, ?)",
                (user_id, title, description, rows, cols)
            )
            note_id = cursor.lastrowid
            cursor.execute(
                "INSERT OR REPLACE INTO user_configs (user_id, rows, cols) VALUES (?, ?, ?)",
                (user_id, rows, cols)
            )
            cursor.execute(
                "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                (user_id, "创建笔记", f"标题: {title}")
            )
            return note_id

        def on_done(worker):
            note_id = worker.get_result()
            if note_id is None:
                return
            self.current_note_id = note_id
            self.update_table_size()
            self.load_user_notes()
            index = self.note_combo.findData(note_id)
            if index >= 0:
                self.note_combo.setCurrentIndex(index)
            if self.history_dock.isVisible():
                self.load_history()
            QMessageBox.information(self, "成功", f"笔记 {title} 创建成功!")

        worker = DBWorker(db_path, do_create)
        self._start_worker(worker, on_finished=on_done)
    
    def save_note(self, silent=False):
        """保存当前笔记（后台线程执行）"""
        if not self.current_user or not self.current_note_id:
            if not silent:
                QMessageBox.warning(self, "警告", "没有可保存的笔记!")
            return

        title = self.note_title_edit.text().strip()
        description = self.note_desc_edit.text().strip()
        note_id = self.current_note_id
        db_path = self.settings.value("database_path", "hex_notes.db")
        user_id = self.current_user[0]
        username = self.current_user[1]

        def do_save(conn):
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE notes SET title = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (title, description, note_id)
            )
            # 记录历史
            cursor.execute(
                "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                (user_id, "保存笔记", f"笔记ID: {note_id}")
            )
            return True

        def on_done(worker):
            result = worker.get_result()
            if result and not silent:
                QMessageBox.information(self, "成功", "笔记已保存!")
            # 更新历史记录显示
            if self.history_dock.isVisible():
                self.load_history()

        worker = DBWorker(db_path, do_save)
        self._start_worker(worker, on_finished=on_done)
    
    def load_note(self):
        """加载笔记（后台线程执行数据库读取）"""
        if not self.current_user:
            QMessageBox.warning(self, "警告", "请先登录!")
            return
            
        note_id = self.note_combo.currentData()
        if not note_id:
            QMessageBox.warning(self, "警告", "请选择要加载的笔记!")
            return

        db_path = self.settings.value("database_path", "hex_notes.db")
        user_id = self.current_user[0]

        def do_load(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, title, description, rows, cols FROM notes WHERE id = ?",
                (note_id,)
            )
            note = cursor.fetchone()
            if note:
                # 更新用户配置
                cursor.execute(
                    "INSERT OR REPLACE INTO user_configs (user_id, rows, cols) VALUES (?, ?, ?)",
                    (user_id, note[3], note[4])
                )
                # 记录历史
                cursor.execute(
                    "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                    (user_id, "加载笔记", f"笔记ID: {note[0]}, 标题: {note[1]}")
                )
            return note

        def on_done(worker):
            note = worker.get_result()
            if note:
                self.current_note_id = note[0]
                self.note_title_edit.setText(note[1])
                self.note_desc_edit.setText(note[2] if note[2] else "")
                self.rows_spin.setValue(note[3])
                self.cols_spin.setValue(note[4])
                self.update_table_size()
                self.load_note_contents()
                self.update_note_tags_display()
                if self.history_dock.isVisible():
                    self.load_history()
                QMessageBox.information(self, "成功", f"笔记 {note[1]} 加载成功!")

        worker = DBWorker(db_path, do_load)
        self._start_worker(worker, on_finished=on_done)
    
    def load_note_contents(self):
        """加载笔记内容（后台线程执行）"""
        if not self.current_note_id:
            return

        note_id = self.current_note_id
        db_path = self.settings.value("database_path", "hex_notes.db")
        rows = self.hex_table.rowCount()
        cols = self.hex_table.columnCount()

        def do_load(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT row, col, content, color FROM note_contents WHERE note_id = ?",
                (note_id,)
            )
            return cursor.fetchall()

        def on_done(worker):
            contents = worker.get_result()
            if contents is None:
                return
            # 清空表格
            for r in range(rows):
                for c in range(cols):
                    item = self.hex_table.item(r, c)
                    if item:
                        item.setText("")
                        item.setBackground(QColor("#FFFFFF"))
            # 填充有内容的单元格
            for row, col, content, color in contents:
                if row < rows and col < cols:
                    item = self.hex_table.item(row, col)
                    if item:
                        item.setText("★" if content else "")
                        item.setBackground(QColor(color) if color else QColor("#FFFFFF"))

        worker = DBWorker(db_path, do_load)
        self._start_worker(worker, on_finished=on_done)
    
    def import_note(self):
        """导入笔记（后台线程执行）"""
        if not self.current_user:
            QMessageBox.warning(self, "警告", "请先登录!")
            return
            
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getOpenFileName(
            self, "导入笔记", "", "文本文件 (*.txt);;所有文件 (*)", options=options
        )
        if not file_path:
            return

        db_path = self.settings.value("database_path", "hex_notes.db")
        user_id = self.current_user[0]

        def do_import(conn):
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if not lines or not lines[0].startswith("笔记标题:"):
                raise ValueError("无效的笔记文件格式")

            title = lines[0][5:].strip()
            description = ""
            if len(lines) > 1 and lines[1].startswith("描述:"):
                description = lines[1][3:].strip()

            rows, cols = 10, 16
            for line in lines:
                if line.startswith("行数:"):
                    parts = line.split(",")
                    if len(parts) == 2 and parts[0].startswith("行数:"):
                        rows = int(parts[0][3:].strip())
                        cols = int(parts[1][3:].strip().split(":")[0])
                        break

            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO notes (user_id, title, description, rows, cols) VALUES (?, ?, ?, ?, ?)",
                (user_id, title, description, rows, cols)
            )
            note_id = cursor.lastrowid

            content_started = False
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if line == "位置说明:":
                    content_started = True
                    continue
                if content_started and line.startswith("[") and "]: " in line:
                    pos_part, content = line.split("]: ", 1)
                    pos_part = pos_part[1:]
                    row, col = map(lambda x: int(x, 16), pos_part.split(","))
                    cursor.execute(
                        "INSERT INTO note_contents (note_id, row, col, content) VALUES (?, ?, ?, ?)",
                        (note_id, row, col, content)
                    )

            # 记录历史
            cursor.execute(
                "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                (user_id, "导入笔记", f"从文件: {file_path}")
            )
            return (note_id, title)

        def on_done(worker):
            result = worker.get_result()
            if result is None:
                return
            note_id, title = result
            self.load_user_notes()
            index = self.note_combo.findData(note_id)
            if index >= 0:
                self.note_combo.setCurrentIndex(index)
                self.load_note()
            if self.history_dock.isVisible():
                self.load_history()
            QMessageBox.information(self, "成功", f"笔记 {title} 导入成功!")

        worker = DBWorker(db_path, do_import)
        self._start_worker(worker, on_finished=on_done)
    
    def export_note(self):
        """导出当前笔记（后台线程执行）"""
        if not self.current_user or not self.current_note_id:
            QMessageBox.warning(self, "警告", "没有可导出的笔记!")
            return
            
        note_title = self.note_combo.currentText()
        options = QFileDialog.Options()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "导出笔记", f"{note_title}.txt",
            "文本文件 (*.txt);;所有文件 (*)", options=options
        )
        if not file_path:
            return

        note_id = self.current_note_id
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_export(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT title, description, rows, cols FROM notes WHERE id = ?",
                (note_id,)
            )
            note_info = cursor.fetchone()
            cursor.execute(
                "SELECT row, col, content FROM note_contents WHERE note_id = ? ORDER BY row, col",
                (note_id,)
            )
            contents = cursor.fetchall()
            cursor.execute(
                "SELECT GROUP_CONCAT(t.name, ', ') FROM tags t JOIN note_tags nt ON t.id = nt.tag_id WHERE nt.note_id = ?",
                (note_id,)
            )
            tags = cursor.fetchone()[0]
            return (note_info, contents, tags)

        def on_done(worker):
            result = worker.get_result()
            if result is None:
                return
            note_info, contents, tags = result

            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(f"笔记标题: {note_info[0]}\n")
                    if note_info[1]:
                        f.write(f"描述: {note_info[1]}\n")
                    f.write(f"行数: {note_info[2]}, 列数: {note_info[3]}\n")
                    if tags:
                        f.write(f"标签: {tags}\n")
                    f.write("\n位置说明:\n")
                    for row, col, content in contents:
                        f.write(f"[{row:02X},{col:02X}]: {content}\n")

                # 在主线程中记录历史（使用原有的db_conn快速写入）
                self.record_history("导出笔记", f"到文件: {file_path}")
                QMessageBox.information(self, "成功", f"笔记已导出到 {file_path}!")
            except Exception as e:
                QMessageBox.critical(self, "错误", f"导出笔记失败: {str(e)}")

        worker = DBWorker(db_path, do_export)
        self._start_worker(worker, on_finished=on_done)
    
    def closeEvent(self, event):
        """关闭事件处理"""
        # 停止自动保存定时器
        self.auto_save_timer.stop()

        # 等待所有活跃的后台线程完成（最多等待3秒）
        for worker in self._active_workers:
            if worker.isRunning():
                worker.wait(3000)
                if worker.isRunning():
                    worker.terminate()

        # 保存当前笔记（同步执行，确保数据不丢失）
        if self.current_note_id and self.current_user:
            try:
                cursor = self.db_conn.cursor()
                title = self.note_title_edit.text().strip()
                description = self.note_desc_edit.text().strip()
                cursor.execute(
                    "UPDATE notes SET title = ?, description = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (title, description, self.current_note_id)
                )
                self.db_conn.commit()
            except Exception:
                pass
        
        # 保存当前设置（关闭时不弹出提示框）
        self.save_settings(show_message=False)
        
        # 关闭数据库连接
        if self.db_conn:
            self.db_conn.close()
        
        event.accept()


    def add_user(self):
        """新建用户并自动登录（后台线程执行）"""
        username, ok = QInputDialog.getText(self, "新建用户", "请输入新用户名:")
        if not ok:
            return
        username = username.strip()
        if not username:
            QMessageBox.warning(self, "警告", "用户名不能为空!")
            return

        password, ok = QInputDialog.getText(self, "设置密码", "请输入密码（可留空表示无密码登录）:", QLineEdit.Password)
        if not ok:
            return

        db_path = self.settings.value("database_path", "hex_notes.db")
        pw_hash = hash_password(password) if password else None

        def do_add(conn):
            cursor = conn.cursor()
            # 检查用户名是否已存在
            cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
            if cursor.fetchone():
                raise sqlite3.IntegrityError(f"用户名 {username} 已存在!")
            cursor.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, pw_hash)
            )
            user_id = cursor.lastrowid
            if self.current_user:
                cursor.execute(
                    "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                    (self.current_user[0], "添加用户", f"用户名: {username}")
                )
            return (user_id, username)

        def on_done(worker):
            result = worker.get_result()
            if result is None:
                return
            user_id, uname = result
            QMessageBox.information(self, "成功", f"用户 {uname} 创建成功!")
            user = (user_id, uname)
            if not self.current_user or self.current_user[0] != user_id:
                self.after_login(user)
            if self.history_dock.isVisible():
                self.load_history()

        def on_error(err_msg):
            QMessageBox.warning(self, "警告", err_msg)

        worker = DBWorker(db_path, do_add)
        self._start_worker(worker, on_finished=on_done, on_error=on_error)

    def change_password(self):
        """设置或修改当前用户的密码（后台线程执行）"""
        if not self.current_user:
            QMessageBox.warning(self, "警告", "请先登录!")
            return

        user_id = self.current_user[0]
        db_path = self.settings.value("database_path", "hex_notes.db")

        # 先获取当前密码状态
        def do_fetch(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT password FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            return row[0] if row else None

        def on_fetched(worker):
            stored = worker.get_result()
            # 已设置密码时，需先验证旧密码
            if stored:
                old_pw, ok = QInputDialog.getText(self, "修改密码", "请输入当前密码:", QLineEdit.Password)
                if not ok:
                    return
                if hash_password(old_pw) != stored:
                    QMessageBox.warning(self, "失败", "当前密码错误!")
                    return

            new_pw, ok = QInputDialog.getText(
                self, "设置密码",
                "请输入新密码（留空表示清除密码，使用无密码登录）:",
                QLineEdit.Password
            )
            if not ok:
                return

            new_hash = hash_password(new_pw) if new_pw else None

            def do_update(conn):
                cursor = conn.cursor()
                cursor.execute("UPDATE users SET password = ? WHERE id = ?", (new_hash, user_id))
                cursor.execute(
                    "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                    (user_id, "修改密码", f"用户名: {self.current_user[1]}")
                )
                return True

            def on_updated(worker):
                if self.history_dock.isVisible():
                    self.load_history()
                QMessageBox.information(self, "成功", "密码已更新!")

            update_worker = DBWorker(db_path, do_update)
            self._start_worker(update_worker, on_finished=on_updated)

        fetch_worker = DBWorker(db_path, do_fetch)
        self._start_worker(fetch_worker, on_finished=on_fetched)

    def delete_note(self):
        """删除当前笔记（后台线程执行）"""
        if not self.current_user or not self.current_note_id:
            QMessageBox.warning(self, "警告", "没有可删除的笔记!")
            return
            
        reply = QMessageBox.question(
            self, "确认删除", "确定要删除当前笔记吗? 此操作不可恢复!",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.No:
            return

        note_id = self.current_note_id
        db_path = self.settings.value("database_path", "hex_notes.db")
        user_id = self.current_user[0]

        def do_delete(conn):
            cursor = conn.cursor()
            cursor.execute("SELECT title FROM notes WHERE id = ?", (note_id,))
            row = cursor.fetchone()
            note_title = row[0] if row else ""
            cursor.execute("DELETE FROM note_contents WHERE note_id = ?", (note_id,))
            cursor.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
            cursor.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            cursor.execute(
                "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                (user_id, "删除笔记", f"标题: {note_title}")
            )
            return note_title

        def on_done(worker):
            note_title = worker.get_result()
            self.current_note_id = None
            self.current_cell = None
            self.note_title_edit.clear()
            self.note_desc_edit.clear()
            self.note_content_edit.clear()
            self.hex_table.clearContents()
            self.note_tags_label.setText("标签: 无")
            self.load_user_notes()
            if self.history_dock.isVisible():
                self.load_history()
            QMessageBox.information(self, "成功", "笔记已删除!")

        worker = DBWorker(db_path, do_delete)
        self._start_worker(worker, on_finished=on_done)

    def delete_current_user(self):
        """删除当前登录的用户（后台线程执行）"""
        if not self.current_user:
            QMessageBox.warning(self, "警告", "当前没有登录用户!")
            return
            
        username = self.current_user[1]
        user_id = self.current_user[0]
        
        if not user_id:
            QMessageBox.warning(self, "警告", "无法删除当前用户!")
            return
            
        reply = QMessageBox.question(
            self, "确认删除", 
            f"确定要删除当前用户 '{username}' 吗?\n这将删除该用户的所有笔记和设置，且无法恢复!",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.No:
            return

        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_delete(conn):
            cursor = conn.cursor()
            cursor.execute("DELETE FROM note_contents WHERE note_id IN (SELECT id FROM notes WHERE user_id = ?)", (user_id,))
            cursor.execute("DELETE FROM note_tags WHERE note_id IN (SELECT id FROM notes WHERE user_id = ?)", (user_id,))
            cursor.execute("DELETE FROM notes WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM tags WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM user_configs WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return True

        def on_done(worker):
            QMessageBox.information(self, "成功", f"用户 '{username}' 已删除!")
            # 重置界面并回到登录界面
            self.logout_user()
            dialog = LoginDialog(self)
            try:
                if dialog.exec() == QDialog.Accepted and dialog.selected_user:
                    self.after_login(dialog.selected_user)
            finally:
                dialog.db_conn.close()

        worker = DBWorker(db_path, do_delete)
        self._start_worker(worker, on_finished=on_done)

    def update_table_size(self):
        """根据当前行数和列数设置更新表格大小"""
        rows = self.rows_spin.value()
        cols = self.cols_spin.value()
        
        # 设置表格行数和列数
        self.hex_table.setRowCount(rows)
        self.hex_table.setColumnCount(cols)
        
        # 设置表头
        self.hex_table.setHorizontalHeaderLabels([f"{i:02X}" for i in range(cols)])
        self.hex_table.setVerticalHeaderLabels([f"{i:02X}" for i in range(rows)])
        
        # 初始化表格单元格
        for row in range(rows):
            for col in range(cols):
                if not self.hex_table.item(row, col):
                    item = QTableWidgetItem("")
                    item.setBackground(QColor("#FFFFFF"))
                    self.hex_table.setItem(row, col, item)
        
        # 如果当前有打开的笔记，重新加载内容
        if self.current_note_id:
            self.load_note_contents()
        
        # 调整列宽
        self.hex_table.resizeColumnsToContents()
        
        # 记录历史
        if self.current_user:
            self.record_history("更新表格大小", f"行数: {rows}, 列数: {cols}")

    def on_cell_clicked(self, row, col):
        """处理表格单元格点击事件（后台读取内容）"""
        self.current_cell = (row, col)
        self.status_label.setText(f"当前位置: [{row:02X},{col:02X}]")
        
        if self.current_note_id:
            note_id = self.current_note_id
            db_path = self.settings.value("database_path", "hex_notes.db")

            def do_load(conn):
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT content FROM note_contents WHERE note_id = ? AND row = ? AND col = ?",
                    (note_id, row, col)
                )
                row_data = cursor.fetchone()
                return row_data[0] if row_data else ""

            def on_done(worker):
                content = worker.get_result()
                if content is not None:
                    self.note_content_edit.setPlainText(content)

            worker = DBWorker(db_path, do_load)
            self._start_worker(worker, on_finished=on_done)
        
        self.note_content_edit.setFocus()

    def save_content(self):
        """保存当前单元格的内容（后台线程执行）"""
        if not self.current_cell or not self.current_note_id:
            QMessageBox.warning(self, "警告", "请先选择单元格!")
            return
            
        row, col = self.current_cell
        content = self.note_content_edit.toPlainText()
        note_id = self.current_note_id
        db_path = self.settings.value("database_path", "hex_notes.db")
        user_id = self.current_user[0]

        def do_save(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM note_contents WHERE note_id = ? AND row = ? AND col = ?",
                (note_id, row, col)
            )
            exists = cursor.fetchone()
            if exists:
                cursor.execute(
                    "UPDATE note_contents SET content = ? WHERE note_id = ? AND row = ? AND col = ?",
                    (content, note_id, row, col)
                )
            else:
                cursor.execute(
                    "INSERT INTO note_contents (note_id, row, col, content) VALUES (?, ?, ?, ?)",
                    (note_id, row, col, content)
                )
            # 记录历史
            cursor.execute(
                "INSERT INTO history (user_id, action, details) VALUES (?, ?, ?)",
                (user_id, "保存内容", f"位置: [{row:02X},{col:02X}]")
            )
            return True

        def on_done(worker):
            # 更新表格显示
            item = self.hex_table.item(row, col)
            if not item:
                item = QTableWidgetItem("★" if content else "")
                item.setBackground(QColor("#FFFFFF"))
                self.hex_table.setItem(row, col, item)
            else:
                item.setText("★" if content else "")
            # 更新历史记录显示
            if self.history_dock.isVisible():
                self.load_history()
            QMessageBox.information(self, "成功", "内容已保存!")

        worker = DBWorker(db_path, do_save)
        self._start_worker(worker, on_finished=on_done)

    def load_user_notes(self):
        """加载当前用户的笔记列表（后台线程执行）"""
        if not self.current_user:
            return

        user_id = self.current_user[0]
        db_path = self.settings.value("database_path", "hex_notes.db")

        def do_load(conn):
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, title FROM notes WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,)
            )
            return cursor.fetchall()

        def on_done(worker):
            notes = worker.get_result()
            if notes is None:
                return
            self.note_combo.clear()
            for note_id, title in notes:
                self.note_combo.addItem(title, note_id)
            if not notes:
                self.note_combo.addItem("无可用笔记", None)

        worker = DBWorker(db_path, do_load)
        self._start_worker(worker, on_finished=on_done)






if __name__ == '__main__':
    app = QApplication(sys.argv)
    
    # 设置应用程序字体，确保中文显示正常
    font = app.font()
    font.setFamily("Microsoft YaHei")  # 可以根据系统调整
    app.setFont(font)
    
    # 先登录，再进入主界面
    login_dialog = LoginDialog()
    if login_dialog.exec() != QDialog.Accepted or not login_dialog.selected_user:
        login_dialog.db_conn.close()
        sys.exit(0)
    
    window = HexNoteApp(initial_user=login_dialog.selected_user)
    login_dialog.db_conn.close()
    
    # 恢复窗口状态
    geometry = window.settings.value("window_geometry")
    if geometry:
        window.restoreGeometry(geometry)
    
    state = window.settings.value("window_state")
    if state:
        window.restoreState(state)
    
    window.show()
    sys.exit(app.exec())