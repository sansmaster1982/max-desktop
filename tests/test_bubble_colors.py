"""Synthetic source/parsing/geometry/rendering regressions; no account data."""
from dataclasses import FrozenInstanceError
import math
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from maxclient.core.appearance import CHAT_THEME_IDS
from maxclient.ui.bubble_colors import (BubbleColors, background_brush,
                                       background_css, bubble_colors, css_qt_color)


class BubbleColorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    # The two development-only corpus/extractor checks are not distributed:
    # their archived WEB inputs and research tools are deliberately excluded.
    # Rendering, immutability, fallback, geometry and color-conversion tests below
    # remain executable without those external provenance fixtures.

    def test_returned_palette_is_frozen_and_not_shared_mutable_state(self):
        colors = bubble_colors('simple', 'dark', True)
        with self.assertRaises(FrozenInstanceError):
            colors.text = '#000'
        self.assertIsInstance(colors.stops, tuple)
        self.assertEqual(colors, bubble_colors('simple', 'dark', True))

    def test_unknown_and_null_presets_fall_back_to_actual_space_in_both_schemes(self):
        for scheme in ('light', 'dark'):
            for outgoing in (False, True):
                for unknown in (None, '', 'SPACE', 'missing', [], {}):
                    with self.subTest(scheme=scheme, outgoing=outgoing, unknown=unknown):
                        self.assertEqual(bubble_colors(unknown, scheme, outgoing),
                                         bubble_colors('space', scheme, outgoing))

    def test_system_scheme_uses_existing_theme_resolver(self):
        for selected in ('dark', 'light'):
            with patch('maxclient.ui.theme.resolve_theme', return_value=selected) as resolve:
                result = bubble_colors('space', 'system', True)
                resolve.assert_called_once_with('system')
            self.assertEqual(result, bubble_colors('space', selected, True))

    def test_css_alpha_is_last_byte_and_short_forms_expand(self):
        expected = {'#fff': (255, 255, 255, 255), '#08c': (0, 136, 204, 255),
                    '#06070885': (6, 7, 8, 133), '#ffffffa3': (255, 255, 255, 163),
                    '#0f8ec214': (15, 142, 194, 20), '#1234': (17, 34, 51, 68),
                    '#aAbBcC': (170, 187, 204, 255)}
        for token, rgba in expected.items():
            with self.subTest(token=token):
                self.assertEqual(css_qt_color(token), f'rgba({rgba[0]}, {rgba[1]}, {rgba[2]}, {rgba[3]})')
                colors = BubbleColors((token, token, token), '#fff', '#fff')
                brush = background_brush(colors, QRectF(0, 0, 100, 20))
                self.assertEqual(brush.gradient().stops()[0][1].getRgb(), rgba)

    def test_css_color_rejects_nonliteral_and_injected_values(self):
        for value in (None, 0, [], 'red', 'transparent', '#12', '#12345', '#1234567', '#123456789',
                      '#ggg', ' #fff', '#fff\n', '#fff; color:red', 'rgba(0,0,0,1)', '##fff'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                css_qt_color(value)

    def test_css_angle_is_239_for_wide_tall_and_square_rectangles(self):
        colors = bubble_colors('simple', 'dark', True)
        for width, height in ((400, 80), (80, 400), (100, 100), (291, 77), (1.25, 3.75)):
            with self.subTest(width=width, height=height):
                rect = QRectF(31, 17, width, height)
                brush = background_brush(colors, rect)
                gradient = brush.gradient()
                start, end = gradient.start(), gradient.finalStop()
                vx, vy = end.x() - start.x(), end.y() - start.y()
                angle = math.degrees(math.atan2(vx, -vy)) % 360
                self.assertAlmostEqual(angle, 239)
                self.assertAlmostEqual((start.x() + end.x()) / 2, rect.center().x())
                self.assertAlmostEqual((start.y() + end.y()) / 2, rect.center().y())
                length = math.hypot(vx, vy)
                self.assertAlmostEqual(length, width * abs(vx / length) + height * abs(vy / length))
                self.assertEqual([position for position, color in gradient.stops()], [0, .5, 1])

    def test_qss_scalar_field_matches_painter_after_aspect_ratio_scaling(self):
        colors = bubble_colors('neon', 'dark', True)
        for width, height in ((400, 80), (80, 400), (291, 77)):
            css = background_css(colors, width, height)
            coords = {key: float(value) for key, value in re.findall(r'(x1|y1|x2|y2):([-+0-9.e]+)', css)}
            brush = background_brush(colors, QRectF(0, 0, width, height))
            gradient = brush.gradient()
            vx, vy = coords['x2'] - coords['x1'], coords['y2'] - coords['y1']
            start, end = gradient.start(), gradient.finalStop()
            dx, dy = end.x() - start.x(), end.y() - start.y()
            for u, v in ((0, 0), (1, 1), (0, 1), (1, 0), (.5, .5), (.2, .7)):
                qss_t = ((u - coords['x1']) * vx + (v - coords['y1']) * vy) / (vx * vx + vy * vy)
                painter_t = ((u * width - start.x()) * dx + (v * height - start.y()) * dy) / (dx * dx + dy * dy)
                self.assertAlmostEqual(qss_t, painter_t, places=10)

    def test_unlaid_out_dimensions_do_not_generate_invalid_qss(self):
        colors = bubble_colors('space', 'light', False)
        for value in (0, -1, None, float('nan'), float('inf'), True):
            css = background_css(colors, value, value)
            self.assertNotIn('nan', css)
            self.assertNotIn('inf', css)
            self.assertEqual(css, background_css(colors, 1, 1))

    def test_bright_presets_use_captured_white_body_and_their_own_reaction_colors(self):
        for preset in ('moscow', 'simple'):
            colors = bubble_colors(preset, 'light', True)
            self.assertEqual(colors.text, '#fff')
            self.assertNotEqual(colors.reaction_bg, '#EAF4FF')
            self.assertIsNotNone(colors.reaction_text)
            self.assertIsNotNone(colors.reaction_my_bg)
            self.assertIsNotNone(colors.reaction_my_text)

    def test_qss_pixels_match_brush_for_resized_bubbles(self):
        for preset, scheme, outgoing in (('simple', 'light', True), ('space', 'dark', False),
                                        ('neon', 'dark', True), ('moscow', 'light', True)):
            colors = bubble_colors(preset, scheme, outgoing)
            for width, height in ((280, 60), (60, 280)):
                with self.subTest(preset=preset, scheme=scheme, size=(width, height)):
                    widget = QWidget()
                    try:
                        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
                        widget.resize(width, height)
                        widget.setStyleSheet('border:none; background:' + background_css(colors, width, height) + ';')
                        actual = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
                        actual.fill(Qt.GlobalColor.transparent)
                        widget.render(actual)
                        expected = QImage(actual.size(), actual.format())
                        expected.fill(Qt.GlobalColor.transparent)
                        painter = QPainter(expected)
                        painter.fillRect(QRectF(0, 0, width, height), background_brush(colors, QRectF(0, 0, width, height)))
                        painter.end()
                        for x, y in ((2, 2), (width // 2, height // 2), (width - 3, height - 3),
                                     (2, height - 3), (width - 3, 2)):
                            got, want = actual.pixelColor(x, y), expected.pixelColor(x, y)
                            self.assertLessEqual(max(abs(a - b) for a, b in zip(got.getRgb(), want.getRgb())), 2)
                    finally:
                        widget.close()
                        widget.deleteLater()


if __name__ == '__main__':
    unittest.main()
