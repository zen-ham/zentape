import sys
import time
import json
import os
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QSpinBox,
                             QLineEdit, QCheckBox, QFileDialog, QFrame,
                             QStackedWidget, QComboBox)
from PyQt5.QtCore import Qt, QTimer, QSize, pyqtSignal, QPropertyAnimation, QEasingCurve, QRect, QThread, pyqtProperty, QRectF, QParallelAnimationGroup, QSequentialAnimationGroup, QObject
from PyQt5.QtGui import QFont, QPalette, QColor, QKeySequence, QIcon, QPixmap, QPainter, QBrush, QPen, QPainterPath
import keyboard

import ctypes, win32con, win32gui, win32process

from utils import StreamController

# --- ASSET PATHS DICTIONARY ---
ASSET_PATHS = {
    'logo': 'assets/app_logo.png',
    'clipping_main_button': 'assets/clipping_icon.png',
    'recording_main_button': 'assets/recording_icon.png',
    'settings_main_button': 'assets/settings_icon.png',
    'minimize_button': 'assets/minimize_icon.png',
    'close_button': 'assets/close_icon.png',
    'clip_now_button': 'assets/clip_now_icon.png',
    'record_start_button': 'assets/record_start_icon.png',
    'record_stop_button': 'assets/record_stop_icon.png',
    'back_button': 'assets/back_icon.png',
    'down_arrow': 'assets/down_arrow.png'  # Added for ComboBox arrow, if you want a custom one
}
# --- END ASSET PATHS DICTIONARY ---

software_name = 'ZenTape'

# --- FONT DEFINITIONS ---
# Ensure "Montserrat" fonts are available on the system.
# On some systems, "Montserrat" might just be "Montserrat Regular".
# "Montserrat Bold" and "Montserrat Black" might be separate font files.
# If they are not found, PyQt will fallback to a default font.
# It's good practice to provide fallbacks in stylesheets if needed.
FONT_MONTSERRAT_NORMAL = QFont("Montserrat", 12, QFont.Normal)
FONT_MONTSERRAT_BOLD = QFont("Montserrat Bold", 12, QFont.Bold)
FONT_MONTSERRAT_BLACK = QFont("Montserrat Black", 12, QFont.Black)

# Specific font usages
FONT_TITLE_BAR_APP_NAME = QFont("Montserrat Bold", 10, QFont.Bold)
FONT_SETTINGS_PAGE_TITLE = QFont("Montserrat Bold", 18, QFont.Bold)
FONT_DROPDOWN_BUTTON_TEXT = QFont("Montserrat Bold", 12, QFont.Bold)
FONT_LABEL_DEFAULT = QFont("Montserrat", 12, QFont.Normal)
FONT_INPUT_WIDGETS = QFont("Montserrat", 12, QFont.Normal)
FONT_SPINBOX = QFont("Montserrat", 12, QFont.Normal)  # Redundant if input widgets cover it, but explicit
FONT_COMBOBOX = QFont("Montserrat", 12, QFont.Normal)  # Redundant if input widgets cover it, but explicit
FONT_CHECKBOX_TEXT = QFont("Montserrat", 12, QFont.Normal)
FONT_SECTION_HEADER = QFont("Montserrat Bold", 12, QFont.Bold)


# --- END FONT DEFINITIONS ---


class SettingsManager:
    """Handles saving and loading application settings"""

    def __init__(self, filename="zen_tape_settings.json"):
        self.filename = filename
        self.default_settings = {
            'clip_hotkey': 'alt+f10',
            'record_hotkey': 'alt+f9',
            'clip_duration': 5,
            'video_fps': 60,
            'audio_sample_rate': 44100,
            'video_bitrate': 5000000,
            'output_directory': 'clips',
            'force_show_notification': True,
            'enabled_streams': [True, True, True],
            'clipping_enabled': True,
            'chosen_mic': None,  # Added for microphone selection
            'record_mouse': True,  # New setting for mouse recording
            'video_resolution': 'Screen' # NEW: Default video resolution
        }

    def load_settings(self):
        """Load settings from file, create with defaults if not exists"""
        # Start with a copy of the defaults to ensure new settings are included.
        settings = self.default_settings.copy()

        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    loaded_settings = json.load(f)
                    # Update the defaults with the user's saved settings from the file.
                    # This adds new default keys and preserves existing user settings.
                    if isinstance(loaded_settings, dict):
                        settings.update(loaded_settings)
            except (json.JSONDecodeError, IOError):
                # If the file is corrupt or unreadable, we'll fall back to defaults.
                pass
        return settings

    def save_settings(self, settings):
        """Save settings to file"""
        try:
            with open(self.filename, 'w') as f:
                json.dump(settings, f, indent=2)
        except IOError:
            print(f"Failed to save settings to {self.filename}")


class HotkeyListener(QThread):
    """Thread for listening to global hotkeys"""
    clip_triggered = pyqtSignal()
    record_triggered = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.clip_hotkey = 'alt+f10'
        self.record_hotkey = 'alt+f9'
        self.running = True
        self._hooks_active = False  # Keep track if hotkeys are currently hooked

    def update_hotkeys(self, clip_hotkey, record_hotkey):
        """Update the hotkey combinations and re-register them with the keyboard library."""
        try:
            # Always unhook all before re-adding to prevent duplicate bindings or stale hotkeys.
            keyboard.unhook_all()
            self._hooks_active = False
        except Exception as e:
            # print(f"Error unhooking all hotkeys: {e}") # For debugging if needed
            pass  # Suppress common 'no hooks active' errors

        self.clip_hotkey = clip_hotkey.lower()
        self.record_hotkey = record_hotkey.lower()

        try:
            keyboard.add_hotkey(self.clip_hotkey, lambda: self.clip_triggered.emit())
            keyboard.add_hotkey(self.record_hotkey, lambda: self.record_triggered.emit())
            self._hooks_active = True
        except Exception as e:
            print(f"Failed to add hotkeys: {e}")

    def run(self):
        """Main thread loop for keeping the hotkey listener alive"""
        # The add_hotkey calls are now handled by update_hotkeys.
        # This loop simply keeps the thread running in the background.
        while self.running:
            time.sleep(0.1)  # Prevents the thread from consuming 100% CPU

    def stop(self):
        """Stop the hotkey listener"""
        self.running = False
        try:
            if self._hooks_active:
                keyboard.unhook_all()
                self._hooks_active = False
        except Exception as e:
            # print(f"Error during final unhooking: {e}") # For debugging if needed
            pass
        self.quit()
        self.wait()


class HotkeyInput(QLineEdit):
    """Custom input field for capturing hotkey combinations"""

    def __init__(self, initial_hotkey=""):
        super().__init__(initial_hotkey)
        self.setReadOnly(True)
        self.setPlaceholderText("Click to set hotkey")
        self.recording = False

    def mousePressEvent(self, event):
        """Start recording hotkey when clicked"""
        if not self.recording:
            self.recording = True
            self.setText("Press key combination...")
            self.setStyleSheet(self.styleSheet() + "background-color: rgba(0, 120, 212, 100);")
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        """Capture key combinations"""
        if not self.recording:
            return

        modifiers = []
        if event.modifiers() & Qt.ControlModifier:
            modifiers.append("ctrl")
        if event.modifiers() & Qt.AltModifier:
            modifiers.append("alt")
        if event.modifiers() & Qt.ShiftModifier:
            modifiers.append("shift")
        if event.modifiers() & Qt.MetaModifier:
            modifiers.append("win")

        key = event.key()
        key_text = ""

        # Handle special keys
        if key == Qt.Key_Escape:
            self.recording = False
            self.setStyleSheet(self.styleSheet().replace("background-color: rgba(0, 120, 212, 100);", ""))
            return
        elif key >= Qt.Key_F1 and key <= Qt.Key_F12:
            key_text = f"f{key - Qt.Key_F1 + 1}"
        elif key >= Qt.Key_0 and key <= Qt.Key_9:
            key_text = str(key - Qt.Key_0)
        elif key >= Qt.Key_A and key <= Qt.Key_Z:
            key_text = chr(key).lower()
        else:
            # Handle other special keys
            special_keys = {
                Qt.Key_Space: "space",
                Qt.Key_Tab: "tab",
                Qt.Key_Return: "enter",
                Qt.Key_Enter: "enter",
                Qt.Key_Backspace: "backspace",
                Qt.Key_Delete: "delete",
                Qt.Key_Insert: "insert",
                Qt.Key_Home: "home",
                Qt.Key_End: "end",
                Qt.Key_PageUp: "page up",
                Qt.Key_PageDown: "page down",
                Qt.Key_Up: "up",
                Qt.Key_Down: "down",
                Qt.Key_Left: "left",
                Qt.Key_Right: "right"
            }
            key_text = special_keys.get(key, "")

        if key_text and (modifiers or key_text in ["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12"]):
            if modifiers:
                hotkey = "+".join(modifiers + [key_text])
            else:
                hotkey = key_text
            self.setText(hotkey)
            self.recording = False
            self.setStyleSheet(self.styleSheet().replace("background-color: rgba(0, 120, 212, 100);", ""))


class CustomTitleBar(QWidget):
    minimizeRequested = pyqtSignal()
    closeRequested = pyqtSignal()
    dragStarted = pyqtSignal()  # New signal for drag

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.parent_window = parent  # Reference to the QMainWindow

        self.setFixedHeight(30)  # Height of the title bar
        # Stylesheet directly affects elements inside this QWidget
        self.setStyleSheet("""
            #titleBar {
                background-color: rgba(25, 25, 25, 200); /* Slightly darker for distinction */
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
                border-bottom: 1px solid #0078d4;
            }
            #titleBar QPushButton {
                background-color: transparent;
                border: none;
                font-size: 14px;
                font-weight: bold;
                color: white;
                padding: 0px;
                min-width: 28px;
                min-height: 28px;
                font-family: 'Montserrat Bold', sans-serif; /* Explicitly set font for buttons */
            }
            #titleBar QPushButton:hover {
                background-color: rgba(0, 120, 212, 100);
            }
            #titleBar QPushButton#closeButton:hover {
                background-color: #e81123; /* Red for close button */
            }
            #titleBar QLabel {
                color: white;
                font-size: 10px; /* Base font size, will be set programmatically too */
                padding-left: 5px;
                font-family: 'Montserrat Bold', sans-serif; /* Explicitly set font for label */
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 0, 5, 0)
        layout.setSpacing(5)

        # Logo
        self.logo_label = QLabel()
        pixmap = QPixmap(ASSET_PATHS['logo']).scaled(18, 18, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.logo_label.setPixmap(pixmap)
        layout.addWidget(self.logo_label)

        # Title
        self.title_label = QLabel(software_name)
        self.title_label.setFont(FONT_TITLE_BAR_APP_NAME)  # Apply Montserrat font
        layout.addWidget(self.title_label)
        layout.addStretch()

        # Minimize button
        self.minimize_btn = QPushButton(QIcon(ASSET_PATHS['minimize_button']), "")
        self.minimize_btn.setObjectName("minimizeButton")
        self.minimize_btn.clicked.connect(self.minimizeRequested.emit)
        layout.addWidget(self.minimize_btn)

        # Close button
        self.close_btn = QPushButton(QIcon(ASSET_PATHS['close_button']), "")
        self.close_btn.setObjectName("closeButton")
        self.close_btn.clicked.connect(self.closeRequested.emit)
        layout.addWidget(self.close_btn)

        self.start_pos = None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.start_pos = event.globalPos()
            self.offset = self.start_pos - self.parent_window.frameGeometry().topLeft()
            self.dragStarted.emit()  # Emit signal when drag starts
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self.start_pos:
            self.parent_window.move(event.globalPos() - self.offset)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.start_pos = None
        self.offset = None
        event.accept()


class DropdownWidget(QWidget):
    """Custom dropdown widget that appears below buttons"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)  # Enable transparency
        # self.setAutoFillBackground(True) # Removed as it interferes with custom paintEvent for translucent background
        self.setStyleSheet("""
            QWidget {
                background-color: rgba(15, 15, 15, 220); /* Semi-transparent black */
                border: 1px solid #0078d4;
                border-radius: 8px;
                color: white;
                font-family: 'Montserrat', sans-serif; /* Default font for dropdown */
            }
            QPushButton {
                background-color: rgba(0, 120, 212, 100);
                border: 1px solid #0078d4;
                border-radius: 6px;
                padding: 8px 16px;
                color: white;
                font-weight: bold;
                min-height: 14px;
                font-family: 'Montserrat Bold', sans-serif;
                font-size: 12px;
                text-align: center; /* Center text vertically with icon if needed */
            }
            QPushButton:hover {
                background-color: rgba(0, 120, 212, 150);
            }
            QPushButton:pressed {
                background-color: rgba(0, 120, 212, 200);
            }
            QPushButton:disabled {
                background-color: rgba(60, 60, 60, 100);
                color: rgba(255, 255, 255, 100);
                border-color: rgba(0, 120, 212, 100);
            }
            QLabel {
                color: #ffffff;
                font-size: 12px;
                font-family: 'Montserrat', sans-serif;
            }
            QSpinBox, QLineEdit, QComboBox {
                background-color: rgba(40, 40, 40, 180);
                border: 1px solid #0078d4;
                border-radius: 4px;
                padding: 4px;
                color: white;
                min-height: 20px;
                font-family: 'Montserrat', sans-serif;
                font-size: 12px;
            }
            QComboBox::drop-down {
                border: 0px; /* Remove the dropdown arrow border */
            }
            QComboBox::down-arrow {
                image: url(assets/down_arrow.png); /* Example: custom arrow icon */
                width: 12px;
                height: 12px;
            }
        """)
        self.hide()

    def paintEvent(self, event):
        # Explicitly draw the background and border
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Define colors and properties from the stylesheet
        background_color = QColor(15, 15, 15, 220)  # rgba(15, 15, 15, 220)
        border_color = QColor(0, 120, 212)  # #0078d4
        border_width = 1
        border_radius = 8

        # Set brush for background
        painter.setBrush(QBrush(background_color))
        # Set pen for border
        painter.setPen(QPen(border_color, border_width))

        # Draw the rounded rectangle.
        # We need to adjust the rectangle to ensure the border is drawn fully within the widget's bounds,
        # otherwise half of the 1px border might be clipped.
        rect_to_draw = self.rect().adjusted(0, 0, -1, -1)
        painter.drawRoundedRect(rect_to_draw, border_radius, border_radius)

        # Call the base class paintEvent to ensure child widgets are drawn
        super().paintEvent(event)


class ClipDropdown(DropdownWidget):
    settings_changed = pyqtSignal()

    def __init__(self, stream_controller, settings, notification_widget):
        super().__init__()
        self.stream_controller = stream_controller
        self.settings = settings
        self.notification_widget = notification_widget
        self.clipping_enabled = settings.get('clipping_enabled', True)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)  # Standardized margins

        # Clip Now button
        self.clip_btn = QPushButton(QIcon(ASSET_PATHS['clip_now_button']), "Clip Now")
        self.clip_btn.setFont(FONT_DROPDOWN_BUTTON_TEXT)
        self.clip_btn.clicked.connect(self.clip_now)
        layout.addWidget(self.clip_btn)

        # Toggle Clipping button
        self.toggle_btn = QPushButton("Toggle Clipping: " + ("Enabled" if self.clipping_enabled else "Disabled"))
        self.toggle_btn.setFont(FONT_DROPDOWN_BUTTON_TEXT)
        self.toggle_btn.clicked.connect(self.toggle_clipping)
        layout.addWidget(self.toggle_btn)

        # Hotkey setting
        hotkey_layout = QHBoxLayout()
        hotkey_label = QLabel("Clip Hotkey:")
        hotkey_label.setFont(FONT_LABEL_DEFAULT)
        hotkey_layout.addWidget(hotkey_label)
        self.hotkey_input = HotkeyInput(self.settings.get('clip_hotkey', 'alt+f10'))
        self.hotkey_input.setFont(FONT_INPUT_WIDGETS)
        self.hotkey_input.textChanged.connect(self.on_hotkey_changed)
        hotkey_layout.addWidget(self.hotkey_input)
        layout.addLayout(hotkey_layout)

        # Duration setting
        duration_layout = QHBoxLayout()
        duration_label = QLabel("Duration (seconds):")
        duration_label.setFont(FONT_LABEL_DEFAULT)
        duration_layout.addWidget(duration_label)
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 3600)
        self.duration_spin.setValue(self.settings.get('clip_duration', 5))
        self.duration_spin.valueChanged.connect(self.update_duration)
        self.duration_spin.setFont(FONT_SPINBOX)
        duration_layout.addWidget(self.duration_spin)
        layout.addLayout(duration_layout)

        # Duration display
        self.duration_label = QLabel()
        self.duration_label.setFont(FONT_LABEL_DEFAULT)
        self.update_duration_display()
        layout.addWidget(self.duration_label)

        self.setLayout(layout)
        self.setFixedSize(250, 200)
        self.update_button_states()

    def toggle_clipping(self):
        """Toggle clipping on/off"""
        self.clipping_enabled = not self.clipping_enabled
        if self.clipping_enabled:
            self.stream_controller.start(True)
            self.toggle_btn.setText("Toggle Clipping: Enabled")
        else:
            self.stream_controller.stop(True)
            self.toggle_btn.setText("Toggle Clipping: Disabled")

        self.update_button_states()
        self.settings['clipping_enabled'] = self.clipping_enabled
        self.settings_changed.emit()

    def update_button_states(self):
        """Update button states based on clipping status"""
        self.clip_btn.setEnabled(self.clipping_enabled)

    def clip_now(self):
        if self.clipping_enabled:
            self.stream_controller.clip()

    def on_hotkey_changed(self):
        """Handle hotkey change"""
        self.settings['clip_hotkey'] = self.hotkey_input.text()
        self.settings_changed.emit()

    def update_duration(self, value):
        self.stream_controller.set_clip_duration(value)
        self.settings['clip_duration'] = value
        self.settings_changed.emit()
        self.update_duration_display()

    def update_duration_display(self):
        seconds = self.duration_spin.value()
        minutes = seconds // 60
        remaining_seconds = seconds % 60

        if minutes > 0:
            time_str = f"{minutes} minute{'s' if minutes != 1 else ''} and {remaining_seconds} second{'s' if remaining_seconds != 1 else ''}"
        else:
            time_str = f"{remaining_seconds} second{'s' if remaining_seconds != 1 else ''}"

        self.duration_label.setText(f"= {time_str}")


class RecordDropdown(DropdownWidget):
    settings_changed = pyqtSignal()

    def __init__(self, stream_controller, settings, notification_widget):
        super().__init__()
        self.stream_controller = stream_controller
        self.settings = settings
        self.notification_widget = notification_widget
        self.recording = False
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)  # Standardized margins

        # Record toggle button
        self.record_btn = QPushButton(QIcon(ASSET_PATHS['record_start_button']), "Start Recording")
        self.record_btn.setFont(FONT_DROPDOWN_BUTTON_TEXT)
        self.record_btn.clicked.connect(self.toggle_recording)
        layout.addWidget(self.record_btn)

        # Hotkey setting
        hotkey_layout = QHBoxLayout()
        hotkey_label = QLabel("Record Hotkey:")
        hotkey_label.setFont(FONT_LABEL_DEFAULT)
        hotkey_layout.addWidget(hotkey_label)
        self.hotkey_input = HotkeyInput(self.settings.get('record_hotkey', 'alt+f9'))
        self.hotkey_input.setFont(FONT_INPUT_WIDGETS)
        self.hotkey_input.textChanged.connect(self.on_hotkey_changed)
        hotkey_layout.addWidget(self.hotkey_input)
        layout.addLayout(hotkey_layout)

        self.setLayout(layout)
        self.setFixedSize(250, 120)

    def on_hotkey_changed(self):
        """Handle hotkey change"""
        self.settings['record_hotkey'] = self.hotkey_input.text()
        self.settings_changed.emit()

    def toggle_recording(self):
        if self.recording:
            self.stream_controller.stop_recording()
            self.record_btn.setIcon(QIcon(ASSET_PATHS['record_start_button']))
            self.record_btn.setText("Start Recording")
            self.recording = False
        else:
            self.stream_controller.start_recording()
            self.record_btn.setIcon(QIcon(ASSET_PATHS['record_stop_button']))
            self.record_btn.setText("Stop Recording")
            self.recording = True


class SettingsPage(QWidget):
    settings_changed = pyqtSignal()

    def __init__(self, stream_controller, settings, notification_widget):
        super().__init__()
        self.stream_controller = stream_controller
        self.settings = settings
        self.notification_widget = notification_widget
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)  # Add margins for content

        # Title (uncommented and styled)
        title = QLabel("Settings")
        title.setFont(FONT_SETTINGS_PAGE_TITLE)  # Apply Montserrat font
        title.setAlignment(Qt.AlignCenter)
        # title.setStyleSheet("color: #0078d4; margin-bottom: 10px;")
        layout.addWidget(title)

        # Video FPS
        fps_layout = QHBoxLayout()
        fps_label = QLabel("Video FPS:")
        fps_label.setFont(FONT_LABEL_DEFAULT)  # Apply font
        fps_layout.addWidget(fps_label)
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["Screen", "15", "30", "60", "75", "90", "100", "120", "144", "165", "180", "240", "360"])
        self.fps_combo.setCurrentText(str(self.settings.get('video_fps', 60)))
        self.fps_combo.currentTextChanged.connect(self.update_fps)
        self.fps_combo.setFont(FONT_COMBOBOX)  # Apply font
        fps_layout.addWidget(self.fps_combo)
        fps_layout.addStretch()
        layout.addLayout(fps_layout)

        # Audio Sample Rate
        sample_layout = QHBoxLayout()
        sample_label = QLabel("Audio Sample Rate:")
        sample_label.setFont(FONT_LABEL_DEFAULT)  # Apply font
        sample_layout.addWidget(sample_label)
        self.sample_combo = QComboBox()
        self.sample_combo.addItems(["22050", "44100", "48000"])
        self.sample_combo.setCurrentText(str(self.settings.get('audio_sample_rate', 44100)))
        self.sample_combo.currentTextChanged.connect(self.update_sample_rate)
        self.sample_combo.setFont(FONT_COMBOBOX)  # Apply font
        sample_layout.addWidget(self.sample_combo)
        sample_layout.addStretch()
        layout.addLayout(sample_layout)

        # Video Bitrate
        bitrate_layout = QHBoxLayout()
        bitrate_label = QLabel("Video Bitrate (bps):")
        bitrate_label.setFont(FONT_LABEL_DEFAULT)  # Apply font
        bitrate_layout.addWidget(bitrate_label)
        self.bitrate_input = QLineEdit(str(self.settings.get('video_bitrate', 5000000)))
        self.bitrate_input.textChanged.connect(self.update_bitrate)
        self.bitrate_input.setFont(FONT_INPUT_WIDGETS)  # Apply font
        bitrate_layout.addWidget(self.bitrate_input)
        bitrate_layout.addStretch()
        layout.addLayout(bitrate_layout)

        # Output Directory
        dir_layout = QHBoxLayout()
        dir_label = QLabel("Output Directory:")
        dir_label.setFont(FONT_LABEL_DEFAULT)  # Apply font
        dir_layout.addWidget(dir_label)
        self.dir_input = QLineEdit(self.settings.get('output_directory', 'clips'))
        self.dir_input.textChanged.connect(self.update_directory)
        self.dir_input.setFont(FONT_INPUT_WIDGETS)  # Apply font
        dir_layout.addWidget(self.dir_input)
        browse_btn = QPushButton("Browse")
        browse_btn.setFont(FONT_DROPDOWN_BUTTON_TEXT)  # Apply font
        browse_btn.clicked.connect(self.browse_directory)
        dir_layout.addWidget(browse_btn)
        layout.addLayout(dir_layout)

        # select mic
        mic_selection_layout = QHBoxLayout()
        mic_label = QLabel("Select Microphone:")
        mic_label.setFont(FONT_LABEL_DEFAULT)
        mic_selection_layout.addWidget(mic_label)

        self.mic_combo = QComboBox()
        self.mic_combo.setFont(FONT_COMBOBOX)

        available_mics = self.stream_controller.list_mics()
        self.mic_combo.addItems(available_mics)

        current_chosen_mic = self.settings.get('chosen_mic', None)
        mic_to_set = None

        if not available_mics:  # No microphones found at all
            mic_to_set = ""  # Effectively no mic
            self.mic_combo.setPlaceholderText("No microphones found")
            self.mic_combo.setEnabled(False)
            self.settings['chosen_mic'] = None  # Ensure setting reflects no mic chosen
        elif current_chosen_mic is None or current_chosen_mic not in available_mics:
            # If no mic chosen yet OR previously chosen mic is no longer available,
            # use the default mic and update settings.
            mic_to_set = self.stream_controller.default_mic()
            # Ensure the default mic is actually in the available list (it should be, but for robustness)
            if mic_to_set not in available_mics and available_mics:
                mic_to_set = available_mics[0]  # Fallback to first available if default is weird
            elif not available_mics:  # Defensive, though already checked by `if not available_mics`
                mic_to_set = ""  # No mics available
            self.settings['chosen_mic'] = mic_to_set
        else:
            # Previously chosen mic is still available
            mic_to_set = current_chosen_mic

        if mic_to_set:
            self.mic_combo.setCurrentText(mic_to_set)
        # If mic_to_set is empty, the placeholder text "No microphones found" will be shown,
        # and the combo box is disabled, which is the desired behavior.

        self.mic_combo.currentTextChanged.connect(self.on_mic_selected)
        mic_selection_layout.addWidget(self.mic_combo)
        mic_selection_layout.addStretch()
        layout.addLayout(mic_selection_layout)

        # NEW: Video Resolution
        resolution_layout = QHBoxLayout()
        resolution_label = QLabel("Video Resolution:")
        resolution_label.setFont(FONT_LABEL_DEFAULT)
        resolution_layout.addWidget(resolution_label)

        self.resolution_combo = QComboBox()
        self.resolution_combo.setFont(FONT_COMBOBOX)
        self.resolution_combo.addItems(["Screen", "144", "240", "360", "480", "720", "1080", "2160", "4320"])
        # Set initial value, defaulting to 'Screen' if not found
        self.resolution_combo.setCurrentText(self.settings.get('video_resolution', 'Screen'))
        self.resolution_combo.currentTextChanged.connect(self.update_resolution)
        resolution_layout.addWidget(self.resolution_combo)
        resolution_layout.addStretch()
        layout.addLayout(resolution_layout)

        # force notification
        self.force_notification = QCheckBox("Force Show Notification")
        self.force_notification.setChecked(self.settings.get('force_show_notification', True))
        self.force_notification.toggled.connect(self.toggle_force_notification)
        self.force_notification.setFont(FONT_CHECKBOX_TEXT)  # Apply font
        layout.addWidget(self.force_notification)

        # New checkbox for mouse recording
        self.record_mouse_check = QCheckBox("Record Mouse Cursor")
        self.record_mouse_check.setChecked(self.settings.get('record_mouse', True))
        self.record_mouse_check.toggled.connect(self.toggle_record_mouse)
        self.record_mouse_check.setFont(FONT_CHECKBOX_TEXT)
        layout.addWidget(self.record_mouse_check)

        # Stream toggles
        stream_label = QLabel("Recording Sources:")
        stream_label.setFont(FONT_SECTION_HEADER)  # Apply Montserrat bold font
        layout.addWidget(stream_label)

        enabled_streams = self.settings.get('enabled_streams', [True, True, True])
        self.video_check = QCheckBox("Record Video")
        self.video_check.setChecked(enabled_streams[0])
        self.video_check.toggled.connect(self.toggle_video)
        self.video_check.setFont(FONT_CHECKBOX_TEXT)  # Apply font
        layout.addWidget(self.video_check)

        self.mic_check = QCheckBox("Record Microphone Audio")
        self.mic_check.setChecked(enabled_streams[1])
        self.mic_check.toggled.connect(self.toggle_mic)
        self.mic_check.setFont(FONT_CHECKBOX_TEXT)  # Apply font
        layout.addWidget(self.mic_check)

        self.sys_check = QCheckBox("Record System Audio")
        self.sys_check.setChecked(enabled_streams[2])
        self.sys_check.toggled.connect(self.toggle_system)
        self.sys_check.setFont(FONT_CHECKBOX_TEXT)  # Apply font
        layout.addWidget(self.sys_check)

        layout.addStretch()

        # Back button
        back_btn = QPushButton(QIcon(ASSET_PATHS['back_button']), "Back")
        back_btn.setFont(FONT_DROPDOWN_BUTTON_TEXT)  # Apply font
        back_btn.clicked.connect(self.go_back)
        layout.addWidget(back_btn)

        self.setLayout(layout)

    def on_mic_selected(self, mic_name):
        """Handle microphone selection change"""
        if mic_name:  # Ensure mic_name is not empty
            self.settings['chosen_mic'] = mic_name
            self.stream_controller.set_mic(mic_name)
            self.settings_changed.emit()
        elif not self.mic_combo.isEnabled():  # If combobox is disabled, it means no mics are available
            self.settings['chosen_mic'] = None
            self.stream_controller.set_mic(None)  # Inform controller that no mic is selected
            self.settings_changed.emit()

    def update_resolution(self, text):
        """Handle video resolution selection change"""
        self.stream_controller.set_resolution(text)
        self.settings['video_resolution'] = text
        self.settings_changed.emit()

    def update_fps(self, text):
        # 'Screen' is passed through as-is; the controller detects the monitor's
        # refresh rate. Everything else is a concrete integer fps.
        value = text if text == 'Screen' else int(text)
        self.stream_controller.set_video_fps(value)
        self.settings['video_fps'] = value
        self.settings_changed.emit()

    def update_sample_rate(self, text):
        self.stream_controller.set_audio_sample_rate(int(text))
        self.settings['audio_sample_rate'] = int(text)
        self.settings_changed.emit()

    def update_bitrate(self, text):
        try:
            bitrate = int(text)
            self.stream_controller.set_bitrate(bitrate)
            self.settings['video_bitrate'] = bitrate
            self.settings_changed.emit()
        except ValueError:
            pass  # Ignore invalid input

    def update_directory(self, text):
        self.stream_controller.set_output_directory(text)
        self.settings['output_directory'] = text
        self.settings_changed.emit()

    def browse_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if directory:
            self.dir_input.setText(directory)
            self.stream_controller.set_output_directory(directory)
            self.settings['output_directory'] = directory
            self.settings_changed.emit()

    def toggle_video(self, checked):
        if checked:
            self.stream_controller.enable_stream_vid()
        else:
            self.stream_controller.disable_stream_vid()
        self.settings['enabled_streams'][0] = checked
        self.settings_changed.emit()

    def toggle_force_notification(self, checked):
        self.notification_widget.force_notification = checked
        self.settings['force_show_notification'] = checked
        self.settings_changed.emit()

    def toggle_record_mouse(self, checked):
        """Toggle recording of the mouse cursor"""
        if checked:
            self.stream_controller.enable_record_mouse()
        else:
            self.stream_controller.disable_record_mouse()
        self.settings['record_mouse'] = checked
        self.settings_changed.emit()

    def toggle_mic(self, checked):
        if checked:
            self.stream_controller.enable_stream_mic()
        else:
            self.stream_controller.disable_stream_mic()
        self.settings['enabled_streams'][1] = checked
        self.settings_changed.emit()

    def toggle_system(self, checked):
        if checked:
            self.stream_controller.enable_stream_sys()
        else:
            self.stream_controller.disable_stream_sys()
        self.settings['enabled_streams'][2] = checked
        self.settings_changed.emit()

    def go_back(self):
        # Correctly access the QMainWindow instance
        self.window().show_main_page()


class MainPage(QWidget):
    def __init__(self, stream_controller, settings, notification_widget):
        super().__init__()
        self.stream_controller = stream_controller
        self.settings = settings
        self.notification_widget = notification_widget
        self.clip_dropdown = None
        self.record_dropdown = None
        self.setup_ui()

    def setup_ui(self):
        layout = QHBoxLayout()
        layout.setSpacing(20)
        layout.setContentsMargins(20, 20, 20, 20)

        # Clip button
        self.clip_btn = QPushButton()
        self.clip_btn.setIcon(QIcon(ASSET_PATHS['clipping_main_button']))
        self.clip_btn.setIconSize(QSize(64, 64))
        self.clip_btn.clicked.connect(self.toggle_clip_dropdown)
        self.style_button(self.clip_btn)
        layout.addWidget(self.clip_btn)

        # Record button
        self.record_btn = QPushButton()
        self.record_btn.setIcon(QIcon(ASSET_PATHS['recording_main_button']))
        self.record_btn.setIconSize(QSize(64, 64))
        self.record_btn.clicked.connect(self.toggle_record_dropdown)
        self.style_button(self.record_btn)
        layout.addWidget(self.record_btn)

        # Settings button
        self.settings_btn = QPushButton()
        self.settings_btn.setIcon(QIcon(ASSET_PATHS['settings_main_button']))
        self.settings_btn.setIconSize(QSize(64, 64))
        self.settings_btn.clicked.connect(self.show_settings)
        self.style_button(self.settings_btn)
        layout.addWidget(self.settings_btn)

        self.setLayout(layout)

        # Create dropdowns
        # Pass notification_widget to ClipDropdown
        self.clip_dropdown = ClipDropdown(self.stream_controller, self.settings, self.notification_widget)
        self.record_dropdown = RecordDropdown(self.stream_controller, self.settings, self.notification_widget)

    def style_button(self, button):
        button.setFixedSize(100, 100)  # Make square
        button.setStyleSheet("""
            QPushButton {
                background-color: rgba(0, 120, 212, 100);
                border: 2px solid #0078d4;
                border-radius: 12px;
                color: white;
                font-family: 'Montserrat Bold', sans-serif;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: rgba(0, 120, 212, 150);
                border-color: #4a9eff;
            }
            QPushButton:pressed {
                background-color: rgba(0, 120, 212, 200);
            }
        """)

    def animate_button(self, button):
        """Animate button press with shrink/grow effect"""
        self.animation = QPropertyAnimation(button, b"geometry")
        self.animation.setDuration(150)
        self.animation.setEasingCurve(QEasingCurve.OutBounce)

        # Get current geometry
        current_rect = button.geometry()

        # Create smaller geometry (shrink)
        shrink_rect = QRect(
            current_rect.x() + 5,
            current_rect.y() + 5,
            current_rect.width() - 10,
            current_rect.height() - 10
        )

        # Set keyframes
        self.animation.setKeyValueAt(0, current_rect)
        self.animation.setKeyValueAt(0.5, shrink_rect)
        self.animation.setKeyValueAt(1, current_rect)

        self.animation.start()

    def toggle_clip_dropdown(self):
        self.animate_button(self.clip_btn)
        if self.clip_dropdown.isVisible():
            self.clip_dropdown.hide()
        else:
            self.hide_all_dropdowns(except_dropdown=self.clip_dropdown)
            self.show_dropdown(self.clip_dropdown, self.clip_btn)

    def toggle_record_dropdown(self):
        self.animate_button(self.record_btn)
        if self.record_dropdown.isVisible():
            self.record_dropdown.hide()
        else:
            self.hide_all_dropdowns(except_dropdown=self.record_dropdown)
            self.show_dropdown(self.record_dropdown, self.record_btn)

    def show_dropdown(self, dropdown, button):
        # Position dropdown below the button, increased Y offset
        button_global_pos = button.mapToGlobal(button.rect().bottomLeft())
        dropdown.move(button_global_pos.x(), button_global_pos.y() + 25)  # Increased from +10 to +25
        dropdown.show()
        dropdown.raise_()

    def hide_all_dropdowns(self, except_dropdown=None):
        if self.clip_dropdown and self.clip_dropdown != except_dropdown:
            self.clip_dropdown.hide()
        if self.record_dropdown and self.record_dropdown != except_dropdown:
            self.record_dropdown.hide()

    def show_settings(self):
        self.animate_button(self.settings_btn)
        self.hide_all_dropdowns()
        # Correctly access the QMainWindow instance
        self.window().show_settings_page()


class ShadowPlayNotification(QWidget):
    # Added signal for thread-safe notification trigger
    notification_triggered = pyqtSignal(int, str, str)

    # --- NEW HELPER METHODS for fullscreen handling ---

    def _is_foreground_fullscreen(self, hwnd):
        """Checks if the given window is fullscreen."""
        try:
            # Get window dimensions
            win_rect = win32gui.GetWindowRect(hwnd)
            win_width = win_rect[2] - win_rect[0]
            win_height = win_rect[3] - win_rect[1]

            # Get screen dimensions
            screen_width = ctypes.windll.user32.GetSystemMetrics(0)
            screen_height = ctypes.windll.user32.GetSystemMetrics(1)

            # A window is considered fullscreen if its dimensions match the screen dimensions.
            # This is a heuristic but works for most exclusive fullscreen applications.
            return win_width == screen_width and win_height == screen_height
        except Exception:
            return False

    def _force_windowed_mode(self):
        """
        If the foreground window is fullscreen, this function forces it into windowed mode
        and stores its original state so it can be restored later.
        """
        # Reset state from any previous run
        self.fullscreen_hwnd = None
        self.original_style = 0
        self.original_rect = (0, 0, 0, 0)

        hwnd = win32gui.GetForegroundWindow()

        # Check if it belongs to our own process to avoid minimizing our own app
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == QApplication.instance().applicationPid():
            return  # It's our own window, do nothing.

        if self._is_foreground_fullscreen(hwnd):
            # It's a fullscreen window. Store its state.
            self.fullscreen_hwnd = hwnd
            self.original_rect = win32gui.GetWindowRect(hwnd)
            self.original_style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)

            # Create a new style by removing popup/borderless styles and adding a caption and border
            new_style = self.original_style
            new_style &= ~win32con.WS_POPUP
            new_style |= win32con.WS_OVERLAPPEDWINDOW

            # Apply the new windowed style
            win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, new_style)

            # Resize the window to be slightly smaller than the screen to ensure it's windowed
            screen_width = ctypes.windll.user32.GetSystemMetrics(0)
            screen_height = ctypes.windll.user32.GetSystemMetrics(1)
            shrink = 0
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST,
                                  1, 1, screen_width - shrink, screen_height - shrink,
                                  win32con.SWP_SHOWWINDOW | win32con.SWP_FRAMECHANGED)
            return True
        return False

    def _restore_fullscreen_mode(self):
        """
        Restores the previously stored window to its original fullscreen state.
        """
        if self.fullscreen_hwnd and self.original_style != 0:
            try:
                # IMPORTANT: Restore the original style first
                win32gui.SetWindowLong(self.fullscreen_hwnd, win32con.GWL_STYLE, self.original_style)

                # Now restore the exact original position and size
                x, y, w, h = self.original_rect
                win32gui.SetWindowPos(self.fullscreen_hwnd, win32con.HWND_TOP,
                                      x, y, w - x, h - y,
                                      win32con.SWP_SHOWWINDOW | win32con.SWP_FRAMECHANGED)
            except Exception as e:
                # The window might have been closed in the meantime
                print(f"Could not restore fullscreen mode: {e}")
            finally:
                # Clear the stored state
                self.fullscreen_hwnd = None
                self.original_style = 0
                self.original_rect = (0, 0, 0, 0)

    # --- END of new helper methods ---

    def get_slide_offset(self):
        return self._slide_offset

    def set_slide_offset(self, offset):
        if self._slide_offset != offset:
            self._slide_offset = offset
            self.update()

    slideOffset = pyqtProperty(int, fget=get_slide_offset, fset=set_slide_offset)

    def __init__(self, seconds=15):
        super().__init__()
        self.seconds = seconds
        self._slide_offset = 0

        # --- NEW instance variables for fullscreen handling ---
        self.force_notification = True
        self.fullscreen_hwnd = None
        self.original_style = 0
        self.original_rect = (0, 0, 0, 0)
        # --- END of new instance variables ---

        self.init_ui()
        self.setup_animation()

        screen = QApplication.desktop().screenGeometry()
        self.final_x = screen.width() - self.width() + 20
        self.y_pos = 50
        self.setGeometry(self.final_x, self.y_pos, self.width(), self.height())

        self.setWindowOpacity(0.0)

        # Connect the thread-safe signal to the actual show_notification method
        self.notification_triggered.connect(self.show_notification)

    def init_ui(self):
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)

        hwnd = self.winId().__int__()
        # Use HWND_TOPMOST with proper flags for fullscreen overlay
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)

        # Set extended window style for layered window and topmost behavior
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex_style |= win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST | win32con.WS_EX_NOACTIVATE
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

        self.setAttribute(Qt.WA_TranslucentBackground)

        self.setFixedSize(320, 80)
        self.visible_drawing_width = self.width() - 20

        layout = QHBoxLayout()
        layout.setContentsMargins(15, 15, 15 + (self.width() - self.visible_drawing_width), 15)
        layout.setSpacing(15)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(40, 40)
        self.create_icon()

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)

        self.main_label = QLabel("INSTANT REPLAY")
        self.main_label.setStyleSheet("""
            QLabel {
                color: #FFFFFF;
                font-size: 12px;
                font-weight: bold;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
        """)

        self.subtitle_label = QLabel(f"{self.seconds}")
        self.subtitle_label.setStyleSheet("""
            QLabel {
                color: #B0C4DE;
                font-size: 11px;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
        """)

        text_layout.addWidget(self.main_label)
        text_layout.addWidget(self.subtitle_label)

        layout.addWidget(self.icon_label)
        layout.addLayout(text_layout)

        self.setLayout(layout)

    def create_icon(self):
        pixmap = QPixmap(40, 40)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        painter.setBrush(QBrush(QColor(30, 144, 255)))
        painter.setPen(QPen(QColor(65, 105, 225), 2))
        painter.drawEllipse(2, 2, 36, 36)

        painter.setBrush(QBrush(QColor(255, 255, 255)))
        painter.setPen(Qt.NoPen)

        triangle = QPainterPath()
        triangle.moveTo(16, 12)
        triangle.lineTo(16, 28)
        triangle.lineTo(28, 20)
        triangle.closeSubpath()

        painter.drawPath(triangle)
        painter.end()

        self.icon_label.setPixmap(pixmap)

    def setup_animation(self):
        slide_duration = 1000  # ms for the slide animation
        fade_duration = 200  # ms for the fade animation

        fade_delay = slide_duration - fade_duration
        if fade_delay < 0:
            fade_delay = 0
            fade_duration = slide_duration

        self.slide_in = QPropertyAnimation(self, b"slideOffset")
        self.slide_in.setDuration(slide_duration)
        self.slide_in.setEasingCurve(QEasingCurve.OutCubic)
        self.slide_in.setStartValue(self.visible_drawing_width)
        self.slide_in.setEndValue(0)

        self.slide_out = QPropertyAnimation(self, b"slideOffset")
        self.slide_out.setDuration(slide_duration)
        self.slide_out.setEasingCurve(QEasingCurve.InCubic)
        self.slide_out.setStartValue(0)
        self.slide_out.setEndValue(self.visible_drawing_width)

        self.fade_in_opacity = QPropertyAnimation(self, b"windowOpacity")
        self.fade_in_opacity.setDuration(fade_duration)
        self.fade_in_opacity.setEasingCurve(QEasingCurve.InQuad)
        self.fade_in_opacity.setStartValue(0.0)
        self.fade_in_opacity.setEndValue(1.0)

        self.fade_out_opacity = QPropertyAnimation(self, b"windowOpacity")
        self.fade_out_opacity.setDuration(fade_duration)
        self.fade_out_opacity.setEasingCurve(QEasingCurve.OutQuad)
        self.fade_out_opacity.setStartValue(1.0)
        self.fade_out_opacity.setEndValue(0.0)

        self.animation_group_in = QParallelAnimationGroup()
        self.animation_group_in.addAnimation(self.slide_in)
        self.animation_group_in.addAnimation(self.fade_in_opacity)

        self.animation_group_out = QParallelAnimationGroup()
        self.animation_group_out.addAnimation(self.slide_out)

        fade_out_delayed_sequence = QSequentialAnimationGroup()
        fade_out_delayed_sequence.addPause(fade_delay)
        fade_out_delayed_sequence.addAnimation(self.fade_out_opacity)
        self.animation_group_out.addAnimation(fade_out_delayed_sequence)

        self.animation_group_out.finished.connect(self._actual_hide)

        self.hide_timer = QTimer()
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_notification)

    def show_notification(self, duration_ms=3000, text='Clipped last 15 seconds', title="INSTANT REPLAY"):

        if self.force_notification:
            self._force_windowed_mode()

        self.main_label.setText(title)
        self.subtitle_label.setText(text)
        self.setWindowOpacity(0.0)
        self.show()

        hwnd = self.winId().__int__()
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)

        self._slide_offset = self.visible_drawing_width

        self.animation_group_in.start()
        self.hide_timer.start(duration_ms)

    def trigger_notification_threadsafe(self, duration_ms=3000, text='Clipped last 15 seconds', title="INSTANT REPLAY"):
        self.notification_triggered.emit(duration_ms, text, title)

    def hide_notification(self):
        if self.animation_group_in.state() == QPropertyAnimation.Running:
            self.animation_group_in.stop()

        self.setWindowOpacity(1.0)
        self._slide_offset = 0

        self.animation_group_out.start()

    def _actual_hide(self):
        self.hide()
        self._slide_offset = self.visible_drawing_width
        self.setWindowOpacity(0.0)

        # --- MODIFICATION START ---
        # After the notification is completely hidden, restore the game to fullscreen.
        self._restore_fullscreen_mode()
        # --- MODIFICATION END ---

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        radius = 8
        border_thickness = 2
        half_border = border_thickness / 2

        visible_width_in_animation = self.visible_drawing_width - self._slide_offset

        x_bg = self._slide_offset
        y_bg = 0
        w_bg = visible_width_in_animation
        h_bg = self.height()
        r_bg = radius
        background_path = QPainterPath()
        background_path.moveTo(x_bg + w_bg, y_bg)
        background_path.lineTo(x_bg + w_bg, y_bg + h_bg)
        background_path.lineTo(x_bg + r_bg, y_bg + h_bg)
        background_path.arcTo(QRectF(x_bg, y_bg + h_bg - 2 * r_bg, 2 * r_bg, 2 * r_bg), 270, -90)
        background_path.lineTo(x_bg, y_bg + r_bg)
        background_path.arcTo(QRectF(x_bg, y_bg, 2 * r_bg, 2 * r_bg), 180, -90)
        background_path.closeSubpath()
        painter.setBrush(QBrush(QColor(0, 0, 0, 200)))
        painter.setPen(Qt.NoPen)
        painter.drawPath(background_path)

        x_b = self._slide_offset + half_border
        y_b = half_border
        w_b = visible_width_in_animation - border_thickness
        h_b = self.height() - border_thickness
        r_b = radius - half_border
        border_path = QPainterPath()
        border_path.moveTo(x_b + w_b, y_b)
        border_path.lineTo(x_b + r_b, y_b)
        border_path.arcTo(QRectF(x_b, y_b, 2 * r_b, 2 * r_b), 90, 90)
        border_path.lineTo(x_b, y_b + h_b - r_b)
        border_path.arcTo(QRectF(x_b, y_b + h_b - 2 * r_b, 2 * r_b, 2 * r_b), 180, 90)
        border_path.lineTo(x_b + w_b, y_b + h_b)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(30, 144, 255, 150), border_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(border_path)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.hide_notification()


class RecordingAppGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        # Initialize settings manager first
        self.settings_manager = SettingsManager()
        self.settings = self.settings_manager.load_settings()

        # Initialize stream controller with loaded settings
        self.stream_controller = StreamController()

        # Initialize notification widget (child of main GUI, but top-level window)
        # Pass the initial clip duration to the notification widget
        self.notification_widget = ShadowPlayNotification(seconds=self.settings['clip_duration'])
        self.notification_widget.force_notification = self.settings.get('force_show_notification', True)
        self.stream_controller.notification_object = self.notification_widget.trigger_notification_threadsafe

        # Ensure it's hidden initially
        self.notification_widget.hide()

        # Initialize hotkey listener
        self.hotkey_listener = HotkeyListener()
        self.hotkey_listener.clip_triggered.connect(self.handle_clip_hotkey)
        self.hotkey_listener.record_triggered.connect(self.handle_record_hotkey)
        # Call update_hotkeys BEFORE starting the thread, so initial hotkeys are registered
        self.update_hotkeys()
        self.hotkey_listener.start()

        self.setup_ui()
        self.setup_window()

        # <<< FIX STARTS HERE >>>
        self.apply_settings_to_controller()
        # <<< FIX ENDS HERE >>>

    def apply_settings_to_controller(self):
        """Apply loaded settings to the stream controller using its setter methods."""
        # Clip duration
        self.stream_controller.set_clip_duration(self.settings['clip_duration'])

        # Video FPS
        self.stream_controller.set_video_fps(self.settings['video_fps'])

        # Audio Sample Rate
        self.stream_controller.set_audio_sample_rate(self.settings['audio_sample_rate'])

        # Video Bitrate
        self.stream_controller.set_bitrate(self.settings['video_bitrate'])

        # Output Directory
        self.stream_controller.set_output_directory(self.settings['output_directory'])

        # Chosen Microphone
        self.stream_controller.set_mic(self.settings.get('chosen_mic'))

        # Video Resolution (NEW)
        self.stream_controller.set_resolution(self.settings.get('video_resolution', 'Screen'))

        # Enabled Streams (Video, Microphone, System Audio)
        enabled_streams = self.settings['enabled_streams']
        if enabled_streams[0]:  # Video
            self.stream_controller.enable_stream_vid()
        else:
            self.stream_controller.disable_stream_vid()

        if enabled_streams[1]:  # Microphone Audio
            self.stream_controller.enable_stream_mic()
        else:
            self.stream_controller.disable_stream_mic()

        if enabled_streams[2]:  # System Audio
            self.stream_controller.enable_stream_sys()
        else:
            self.stream_controller.disable_stream_sys()

        # Mouse recording status
        if self.settings['record_mouse']:
            self.stream_controller.enable_record_mouse()
        else:
            self.stream_controller.disable_record_mouse()

        # Clipping Enabled status
        if self.settings['clipping_enabled']:
            self.stream_controller.start(True)
        else:
            # Explicitly stop if it's disabled, in case it was running from a previous session
            # and the setting changed to disabled.
            self.stream_controller.stop(True)

    def update_hotkeys(self):
        """Update hotkey listener with current hotkeys"""
        clip_hotkey = self.settings.get('clip_hotkey', 'alt+f10')
        record_hotkey = self.settings.get('record_hotkey', 'alt+f9')
        self.hotkey_listener.update_hotkeys(clip_hotkey, record_hotkey)

    def handle_clip_hotkey(self):
        """Handle clip hotkey press"""
        # This slot is executed in the GUI thread.
        # Calling clip_now() directly from main_page.clip_dropdown is safe.
        if hasattr(self.main_page, 'clip_dropdown') and self.settings.get('clipping_enabled', True):
            self.main_page.clip_dropdown.clip_now()

    def handle_record_hotkey(self):
        """Handle record hotkey press"""
        # This slot is executed in the GUI thread.
        # Calling toggle_recording() directly from main_page.record_dropdown is safe.
        if hasattr(self.main_page, 'record_dropdown'):
            self.main_page.record_dropdown.toggle_recording()

    def save_settings(self):
        """Save current settings to file"""
        self.settings_manager.save_settings(self.settings)

    def setup_ui(self):
        # Main container for transparent background and border
        self.main_container = QFrame(self)  # Use QFrame for easier styling
        self.main_container.setObjectName("mainContainer")
        self.setCentralWidget(self.main_container)  # This is now the central widget

        main_layout = QVBoxLayout(self.main_container)
        main_layout.setContentsMargins(0, 0, 0, 0)  # No margins for the container
        main_layout.setSpacing(0)

        # Original QStackedWidget, now inside content_wrapper
        self.central_widget = QStackedWidget()

        # Main page - pass notification_widget
        self.main_page = MainPage(self.stream_controller, self.settings, self.notification_widget)
        self.central_widget.addWidget(self.main_page)

        # Connect settings changed signals
        self.main_page.clip_dropdown.settings_changed.connect(self.on_settings_changed)
        self.main_page.record_dropdown.settings_changed.connect(self.on_settings_changed)

        # Custom Title Bar
        self.title_bar = CustomTitleBar(self)
        self.title_bar.minimizeRequested.connect(self.on_minimize)
        self.title_bar.closeRequested.connect(self.close)
        self.title_bar.dragStarted.connect(self.main_page.hide_all_dropdowns)  # Connect drag signal
        main_layout.addWidget(self.title_bar)

        # Content wrapper for the main application pages
        self.content_wrapper = QWidget()
        content_wrapper_layout = QVBoxLayout(self.content_wrapper)
        content_wrapper_layout.setContentsMargins(0, 0, 0, 0)
        content_wrapper_layout.setSpacing(0)

        content_wrapper_layout.addWidget(self.central_widget)  # Add stacked widget to content wrapper

        main_layout.addWidget(self.content_wrapper)  # Add content wrapper to main container layout

        # Settings page
        self.settings_page = SettingsPage(self.stream_controller, self.settings, self.notification_widget)
        self.settings_page.settings_changed.connect(self.on_settings_changed)
        self.central_widget.addWidget(self.settings_page)

        self.central_widget.setCurrentWidget(self.main_page)

    def on_minimize(self):
        self.main_page.hide_all_dropdowns()
        self.showMinimized()

    def on_settings_changed(self):
        """Handle settings changes - save and update hotkeys"""
        self.save_settings()
        self.update_hotkeys()
        # The subtitle_label of notification_widget will be updated dynamically when clip_now is called.

    def setup_window(self):
        self.setWindowTitle("Recording App")

        # Make window transparent and frameless (removed WindowStaysOnTopHint)
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Set dark blue theme
        # General QApplication style settings for font family
        # This sets a base font, but explicit widget.setFont() and stylesheet rules take precedence.
        QApplication.instance().setFont(FONT_MONTSERRAT_NORMAL)

        self.setStyleSheet(f"""
            QMainWindow {{ background-color: transparent; }} /* QMainWindow itself is fully transparent */
            #mainContainer {{
                background-color: rgba(15, 15, 15, 220);
                border: 1px solid #0078d4;
                border-radius: 12px;
            }}
            QWidget {{ /* Default for other widgets inside mainContainer, can be transparent */
                background-color: transparent;
                color: white;
                font-family: 'Montserrat', sans-serif; /* Fallback for widgets not explicitly set */
            }}
            QLabel {{
                color: white;
                font-size: 12px; /* Base size, individual labels set by FONT_LABEL_DEFAULT */
                font-family: 'Montserrat', sans-serif;
            }}
            QPushButton {{ /* General button style, can be overridden */
                background-color: rgba(0, 120, 212, 100);
                border: 1px solid #0078d4;
                border-radius: 6px;
                padding: 8px 12px;
                color: white;
                font-weight: bold;
                font-family: 'Montserrat Bold', sans-serif;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: rgba(0, 120, 212, 150);
            }}
            QPushButton:pressed {{
                background-color: rgba(0, 120, 212, 200);
            }}
            QSpinBox, QLineEdit, QComboBox {{
                background-color: rgba(40, 40, 40, 180);
                border: 1px solid #0078d4;
                border-radius: 4px;
                padding: 6px;
                color: white;
                min-height: 14px;
                font-family: 'Montserrat', sans-serif;
                font-size: 12px;
            }}
            QComboBox::drop-down {{
                border: 0px; /* Remove the dropdown arrow border */
            }}
            QComboBox::down-arrow {{
                image: url({ASSET_PATHS['down_arrow']}); /* Example: custom arrow icon */
                width: 12px;
                height: 12px;
            }}
            QCheckBox {{
                color: white;
                spacing: 8px;
                font-family: 'Montserrat', sans-serif;
                font-size: 12px;
            }}
            QCheckBox::indicator {{
                width: 18px;
                height: 18px;
                border-radius: 3px;
                border: 1px solid #0078d4;
                background-color: rgba(40, 40, 40, 180);
            }}
            QCheckBox::indicator:checked {{
                background-color: #0078d4;
            }}
        """)

        # Initial size needs to account for title bar now
        self.setFixedSize(400, 140 + self.title_bar.height())
        # Center the window
        self.center_on_screen()

    def center_on_screen(self):
        screen = QApplication.desktop().screenGeometry()
        size = self.geometry()
        self.move(
            (screen.width() - size.width()) // 2,
            (screen.height() - size.height()) // 2
        )

    def show_settings_page(self):
        # Increased height for new microphone section, mouse record checkbox, and now video resolution dropdown
        self.setFixedSize(400, 640 + self.title_bar.height()) # Adjusted height from 595 to 640
        self.central_widget.setCurrentWidget(self.settings_page)
        self.main_page.hide_all_dropdowns()

    def show_main_page(self):
        self.setFixedSize(400, 140 + self.title_bar.height())
        self.central_widget.setCurrentWidget(self.main_page)

    def closeEvent(self, event):
        """Clean up when closing"""
        # Stop hotkey listener
        if hasattr(self, 'hotkey_listener'):
            self.hotkey_listener.stop()

        # Save settings one final time
        self.save_settings()

        # Close dropdowns
        if hasattr(self.main_page, 'clip_dropdown') and self.main_page.clip_dropdown:
            self.main_page.clip_dropdown.close()
        if hasattr(self.main_page, 'record_dropdown') and self.main_page.record_dropdown:
            self.main_page.record_dropdown.close()
        # Close the notification widget
        if hasattr(self, 'notification_widget'):
            self.notification_widget.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    window = RecordingAppGUI()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()