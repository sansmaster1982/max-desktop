"""Actual message bubbles share captured colors with the synthetic preview."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from maxclient.core.models import Attach, Message
from maxclient.ui.bubble_colors import bubble_colors, css_qt_color
from maxclient.ui.message_links import set_literal_message_text
from maxclient.ui.theme import build_stylesheet, palette_for
from maxclient.ui.widgets.message_bubble import MessageBubble
from test_desktop_shell import _load_offscreen_font


class BubbleThemeRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        _load_offscreen_font(cls.app)

    def setUp(self):
        self.host = QWidget()
        self.host.setStyleSheet(build_stylesheet('light', '#0085FF'))
        self.layout = QVBoxLayout(self.host)
        self.host.resize(600, 400)

    def tearDown(self):
        self.host.close()
        self.host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def make(self, preset, outgoing=True, *, scheme='light', attaches=None, reactions=None):
        msg = Message(id=91, chat_id=42, time_ms=123456789, outgoing=outgoing,
                      text='Пример <b>буквальный</b> https://example.test/test?a=1&b=2',
                      attaches=attaches or [], reaction_info=reactions or {})
        bubble = MessageBubble(msg, '#0085FF', palette_for(scheme), chat_preset=preset,
                               file_actions={0: {'download_available': True, 'file_name': 'sample.txt'}})
        self.layout.addWidget(bubble)
        self.host.show()
        self.app.processEvents()
        return bubble, bubble.findChild(QFrame, 'Bubble')

    def test_actual_outgoing_body_and_footer_follow_preset(self):
        for preset in ('feb23', 'mar8', 'moscow', 'simple'):
            with self.subTest(preset=preset):
                widget, frame = self.make(preset)
                colors = bubble_colors(preset, 'light', True)
                self.assertEqual(widget._conversation_colors, colors)
                for color in colors.stops:
                    self.assertIn(css_qt_color(color), frame.styleSheet())
                text = widget.findChild(QLabel, 'MessageText')
                self.assertIn('&lt;b&gt;', text.text())
                self.assertIn(css_qt_color(colors.link_text), text.text())
                self.assertTrue(any(css_qt_color(colors.time) in label.styleSheet()
                                    for label in widget.findChildren(QLabel)))

    def test_gradient_reflows_without_losing_search_highlight(self):
        widget, frame = self.make('simple', scheme='dark')
        frame.set_spotlight(True)
        before = frame.property('searchBaseStyle')
        frame.resize(330, 260)
        self.app.processEvents()
        self.assertIn('border:2px solid #0085FF', frame.styleSheet())
        self.assertIn('qlineargradient(', frame.styleSheet())
        self.assertNotEqual(frame.property('searchBaseStyle'), before)
        frame.set_spotlight(False)
        self.assertNotIn('border:2px', frame.styleSheet())
        self.assertIn('qlineargradient(', frame.styleSheet())

    def test_reactions_and_file_hover_preserve_contrast_on_white_text_theme(self):
        reactions = {'counters': [{'reaction': '👍', 'count': 2}, {'reaction': '❤️', 'count': 1}],
                     'yourReaction': '👍'}
        widget, _ = self.make('moscow', attaches=[Attach(type='FILE', file_name='sample.txt')], reactions=reactions)
        colors = widget._conversation_colors
        chips = widget.findChildren(QPushButton, 'MessageReaction')
        self.assertEqual(len(chips), 2)
        for chip in chips:
            selected = chip.isChecked()
            self.assertIn(css_qt_color(colors.reaction_my_text if selected else colors.reaction_text), chip.styleSheet())
            self.assertIn(css_qt_color(colors.reaction_my_bg if selected else colors.reaction_bg), chip.styleSheet())
        primary = widget.findChild(QPushButton, 'MessageFilePrimary')
        self.assertIn('rgba(0,0,0,0.06)', primary.styleSheet())
        self.assertNotIn(palette_for('light').hover, primary.styleSheet())
        more = widget.findChild(QPushButton, 'MessageFileMore')
        pixels = more.icon().pixmap(20, 20).toImage()
        self.assertTrue(any(pixels.pixelColor(x, y).alpha() > 0
                            for x in range(pixels.width()) for y in range(pixels.height())))

    def test_literal_link_theme_color_is_rendered_and_style_injection_is_rejected(self):
        label = QLabel(self.host)
        set_literal_message_text(label, 'https://example.test/', link_color='rgba(255, 255, 255, 255)')
        document = QTextDocument()
        document.setHtml(label.text())
        cursor = document.find('https://example.test/')
        self.assertEqual(cursor.charFormat().foreground().color().name(), '#ffffff')
        set_literal_message_text(label, 'https://example.test/', link_color='red" onclick="bad')
        self.assertNotIn('onclick', label.text())
        self.assertIn('color:#0085FF;', label.text())


if __name__ == '__main__':
    unittest.main()
