"""Подготовка фото к отправке: пересжатие без метаданных.

Исходный файл фото несёт EXIF: координаты съёмки, модель камеры, время. На сервер
эти сведения уходить не должны. Официальный веб-клиент web.max.ru перед отправкой пересжимает
изображение библиотекой browser-image-compression: наибольшая сторона 1920 px,
качество 0.8, preserveExif выключен. GIF, анимированные изображения и файлы, которые
сжать не удалось, он отправляет как есть. Модуль повторяет это средствами Qt: Pillow
в зависимостях проекта нет.

Порядок действий. Поведение Qt проверено на PySide6 6.11.1.

1. Формат берётся из содержимого файла (QImageReader.format()), расширение не
   учитывается. Обрабатываются JPEG, PNG и статичный WEBP. Остальные форматы и
   анимация возвращаются исходным путём.
2. Файл открывает сам модуль и передаёт читателю устройство. QImageReader(имя) к
   несуществующему имени подставляет известные расширения и читает соседний файл.
3. Ориентацию JPEG применяет Qt (setAutoTransform). Из блока eXIf в PNG и блока
   EXIF в WEBP Qt ориентацию не читает, поэтому тег разбирается здесь и применяется
   к пикселям по той же таблице. APNG для Qt выглядит обычным PNG; анимация видна
   по блоку acTL.
4. JPEG больше 1920 px декодируется сразу уменьшенным (setScaledSize): libjpeg
   масштабирует при декодировании, полный кадр в памяти не нужен. PNG и WEBP
   уменьшает QImage.scaled со сглаживанием. Меньшая сторона округляется вниз, как у
   canvas в браузере.
5. Профиль, отличный от sRGB, пересчитывается в sRGB до смены формата пикселей.
   В результат профиль не записывается.
6. Изображение с прозрачностью проходит через предумноженную альфу, как пиксели
   canvas в браузере: цвет, записанный под полностью прозрачными пикселями (фон,
   скрытый маской), обнуляется. Без уменьшения это делается в 16 битах на канал:
   альфа и цвет видимых пикселей возвращаются точно, кроме пикселей с альфой 1 из
   255, где цвет может сдвинуться на единицу.
7. QImageWriter переносит в файл текстовые ключи QImage (COM у JPEG, tEXt и iTXt у
   PNG), а setText(key, "") ключ не удаляет. Поэтому пиксели копируются через
   QPainter в новое изображение: у него нет ключей, профиля и исходного разрешения
   печати.
8. Результат кодируется в память и записывается одним файлом со случайным именем.
   При сбое на любом шаге во workdir ничего не остаётся.
"""
from __future__ import annotations

import os
import struct
import uuid
from typing import Any, Callable, Optional

PHOTO_MAX_SIDE = 1920
PHOTO_JPEG_QUALITY = 80

_EXTENSIONS = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Orientation лежит в IFD0, в начале блока; миниатюру и MakerNote читать незачем.
_EXIF_READ_LIMIT = 64 * 1024


class _Keep(Exception):
    """Отправлять исходный файл. Текст уходит в журнал, поэтому в нём только причина."""


def prepare_photo(path: str, workdir: str, *, log: Optional[Callable[[str], None]] = None) -> str:
    """Вернуть путь файла, который нужно отправить вместо фото path.

    Это новый файл внутри существующего каталога workdir (удаляет его вызывающий)
    либо сам path, если фото не обрабатывается или обработка не удалась. Исключений
    функция не бросает. Вызывать её можно из рабочего потока. В строку log попадают
    формат, размеры в пикселях и байтах и причина отказа; пути, имени файла и значений
    метаданных в ней нет.
    """
    try:
        result, line = _prepare(path, workdir)
    except _Keep as reason:
        result, line = path, f"photo prepare: kept, {reason}"
    except Exception as error:  # noqa: BLE001 - подготовка не должна сорвать отправку
        # Только имя класса: текст ошибки ОС содержит путь к файлу.
        result, line = path, f"photo prepare: kept, error={type(error).__name__}"
    if log is not None:
        try:
            log(line)
        except Exception:  # noqa: BLE001 - журнал не влияет на результат
            pass
    return result


def _prepare(path: str, workdir: str) -> tuple[str, str]:
    # PySide6 импортируется здесь, чтобы модуль загружался без Qt.
    from PySide6.QtCore import QBuffer, QFile, QIODevice, QSize, Qt
    from PySide6.QtGui import QColorSpace, QImage, QImageIOHandler, QImageReader, QImageWriter, QPainter

    # Только строковый путь: число open() принял бы за открытый дескриптор и закрыл бы его.
    file_name = os.fspath(path)
    if not isinstance(file_name, str):
        raise _Keep("stage=open")
    animated, orientation = _container_facts(file_name)
    source = QFile(file_name)
    if not source.open(QIODevice.OpenModeFlag.ReadOnly):
        raise _Keep("stage=open")
    try:
        reader = QImageReader(source)
        reader.setDecideFormatFromContent(True)
        kind = bytes(reader.format()).decode("ascii", "replace").lower()
        kind = "jpeg" if kind in ("jpg", "jfif") else kind
        if kind not in _EXTENSIONS:
            raise _Keep(f"format={kind or 'unknown'}")
        if kind == "webp" and b"webp" not in {bytes(name) for name in QImageWriter.supportedImageFormats()}:
            raise _Keep("format=webp without a writer")
        if animated or (reader.supportsAnimation() and reader.imageCount() > 1):
            raise _Keep(f"animated {kind}")
        if reader.supportsOption(QImageIOHandler.ImageOption.ImageTransformation):
            orientation = 1  # тег применит сам Qt
        reader.setAutoTransform(True)
        header = reader.size()
        if kind == "jpeg" and max(header.width(), header.height()) > PHOTO_MAX_SIDE:
            reader.setScaledSize(QSize(*_fit(header.width(), header.height())))
        image = reader.read()
        if image.isNull():
            raise _Keep(f"stage=read {reader.error().name}")
        source_bytes = source.size()
    finally:
        source.close()

    # Имени вида photo@2x.jpg QImageReader ставит devicePixelRatio 2, и QPainter
    # нарисовал бы такое фото в четверть площади.
    image.setDevicePixelRatio(1.0)
    srgb = QColorSpace(QColorSpace.NamedColorSpace.SRgb)
    space = image.colorSpace()
    if space.isValid() and space != srgb:
        image.convertToColorSpace(srgb)
    alpha = image.hasAlphaChannel()
    final = QImage.Format.Format_ARGB32 if alpha else QImage.Format.Format_RGB32
    # Кадр с прозрачностью проходит через предумноженную альфу, как пиксели canvas в
    # браузере: цвет под полностью прозрачными пикселями обнуляется и с фото не уходит.
    width, height = _fit(image.width(), image.height())
    if (width, height) != (image.width(), image.height()):
        # В этих двух форматах Qt уменьшает усреднением по площади. convertTo меняет
        # формат на месте, без второй копии большого кадра.
        image.convertTo(QImage.Format.Format_ARGB32_Premultiplied if alpha else final)
        image = image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    elif alpha:
        # 16 бит на канал: цвет видимых пикселей возвращается из предумноженного вида
        # без потерь. В 8 битах он менялся бы у двух третей сочетаний цвета и альфы.
        image.convertTo(QImage.Format.Format_RGBA64_Premultiplied)
    image.convertTo(final)
    image = _oriented(image, orientation)
    if image.isNull():
        raise _Keep("stage=convert")

    clean = QImage(image.size(), final)
    if clean.isNull():
        raise _Keep("stage=paint")
    clean.fill(0)  # память нового QImage не обнулена; в файл она попасть не должна ни при каком исходе
    painter = QPainter()
    if not painter.begin(clean):
        raise _Keep("stage=paint")
    # Source при совпадающих форматах копирует пиксели без смешивания, в том числе прозрачные.
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    painter.drawImage(0, 0, image)
    painter.end()

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QImageWriter(buffer, kind.encode("ascii"))
    if kind != "png":  # у PNG это число задаёт степень сжатия zlib, потерь там нет
        writer.setQuality(PHOTO_JPEG_QUALITY)
    if kind == "jpeg":
        writer.setOptimizedWrite(True)  # таблицы Хаффмана под кадр: меньше байт при тех же пикселях
    if not writer.write(clean):
        raise _Keep("stage=encode")
    data = bytes(buffer.data())
    if not data:
        raise _Keep("stage=encode")

    target = os.path.join(workdir, uuid.uuid4().hex + _EXTENSIONS[kind])
    _store(target, data)
    return target, (f"photo prepare: {kind} {header.width()}x{header.height()} -> "
                    f"{clean.width()}x{clean.height()}, {source_bytes} -> {len(data)} bytes")


def _fit(width: int, height: int) -> tuple[int, int]:
    """Размер после уменьшения до PHOTO_MAX_SIDE по наибольшей стороне; увеличения нет."""
    longest = max(width, height)
    if longest <= PHOTO_MAX_SIDE:
        return width, height
    return max(1, width * PHOTO_MAX_SIDE // longest), max(1, height * PHOTO_MAX_SIDE // longest)


def _container_facts(path: str) -> tuple[bool, int]:
    """(анимация, ориентация EXIF) из блоков PNG или WEBP; для прочих файлов (False, 1)."""
    animated, exif = False, b""
    with open(path, "rb") as handle:
        magic = handle.read(12)
        if magic[:8] == _PNG_SIGNATURE:
            handle.seek(8)
            while True:
                head = handle.read(8)
                if len(head) < 8:
                    break
                length, name = struct.unpack(">I4s", head)
                if name == b"IEND":
                    break
                if name == b"acTL":  # APNG: число кадров анимации
                    animated = int.from_bytes(handle.read(4), "big") > 1
                    handle.seek(length, 1)  # остаток блока и CRC
                elif name == b"eXIf":
                    exif = handle.read(min(length, _EXIF_READ_LIMIT))
                    handle.seek(length - len(exif) + 4, 1)
                else:
                    handle.seek(length + 4, 1)
        elif magic[:4] == b"RIFF" and magic[8:12] == b"WEBP":
            while True:
                head = handle.read(8)
                if len(head) < 8:
                    break
                name, length = struct.unpack("<4sI", head)
                if name == b"EXIF":
                    exif = handle.read(min(length, _EXIF_READ_LIMIT))
                    break
                handle.seek(length + (length & 1), 1)
    return animated, _exif_orientation(exif)


def _exif_orientation(blob: bytes) -> int:
    """Тег Orientation (0x0112) из IFD0 блока TIFF. 1, если тега нет или блок не разобран."""
    if blob[:6] == b"Exif\x00\x00":  # в WEBP блок встречается с префиксом сегмента JPEG
        blob = blob[6:]
    order = {b"II*\x00": "<", b"MM\x00*": ">"}.get(blob[:4])
    if order is None:
        return 1
    try:
        offset = struct.unpack(order + "I", blob[4:8])[0]
        count = struct.unpack(order + "H", blob[offset:offset + 2])[0]
        for index in range(count):
            start = offset + 2 + 12 * index
            tag, kind, items, value = struct.unpack(order + "HHIH", blob[start:start + 10])
            if tag == 0x0112:
                return value if kind == 3 and items == 1 and 1 <= value <= 8 else 1
    except struct.error:
        pass
    return 1


def _oriented(image: Any, orientation: int) -> Any:
    """Применить ориентацию EXIF к пикселям. Таблица та же, что у Qt для JPEG."""
    if orientation not in (2, 3, 4, 5, 6, 7, 8):
        return image
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTransform

    if orientation in (2, 7):
        image = _flipped(image, Qt.Orientation.Horizontal)
    elif orientation in (4, 5):
        image = _flipped(image, Qt.Orientation.Vertical)
    turn = {3: 180, 5: 90, 6: 90, 7: 90, 8: 270}.get(orientation)
    if turn:
        # Повороты на 90, 180 и 270 градусов Qt выполняет перестановкой пикселей, без интерполяции.
        image = image.transformed(QTransform().rotate(turn))
    return image


def _flipped(image: Any, axis: Any) -> Any:
    from PySide6.QtCore import Qt

    flipped = getattr(image, "flipped", None)  # Qt 6.9+; mirrored() с этой версии объявлен устаревшим
    if flipped is not None:
        return flipped(axis)
    return image.mirrored(axis == Qt.Orientation.Horizontal, axis == Qt.Orientation.Vertical)


def _store(target: str, data: bytes) -> None:
    """Записать результат целиком либо не оставить файла."""
    created = done = False
    try:
        with open(target, "xb") as handle:
            created = True
            handle.write(data)
        done = True
    except OSError:
        raise _Keep("stage=write") from None
    finally:
        if created and not done:
            try:
                os.remove(target)
            except OSError:
                pass
