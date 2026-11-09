"""User-initiated web links; rendering never fetches a preview or invokes RPC."""
import re
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices

from ..core.message_presentation import literal_link_html, safe_web_url


def open_web_link(value):
    target = safe_web_url(value)
    if target is not None:
        return QDesktopServices.openUrl(QUrl(target))
    return False


def set_literal_message_text(label, text, *, link_color=None):
    rich = literal_link_html(text)
    # The optional theme color comes from pinned UI constants, not HTML from
    # message data. Still restrict the interpolation to a literal color token.
    if (rich and isinstance(link_color, str)
            and re.fullmatch(r'(?:#[0-9a-fA-F]{6}|rgba\(\d{1,3},\s*\d{1,3},\s*\d{1,3},\s*(?:\d{1,3}|0?\.\d+|1\.0+)\))', link_color)):
        rich = rich.replace('style="color:#0085FF;', 'style="color:' + link_color + ';')
    label.setOpenExternalLinks(False)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    label.setText(rich or text)
    flags = Qt.TextInteractionFlag.TextSelectableByMouse
    if rich:
        flags |= Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        label.linkActivated.connect(open_web_link)
    label.setTextInteractionFlags(flags)
