# Qt / PySide notices for the MAXIM portable alpha

Prepared 2026-11-09 for PySide6, PySide6-Essentials, PySide6-Addons and Shiboken6 **6.11.1**, using the installed package metadata and official upstream `v6.11.1` sources. This appendix records provenance and coverage; it is not a legal certification or a claim that every redistribution obligation has been discharged.

## Open-source terms

The installed PySide6 metadata states `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`. A packaged `LicenseRef-Qt-Commercial.txt` reference does not replace those explicitly offered open-source alternatives. Qt modules and their third-party components retain their respective licenses; the application's own license does not relicense them.

The full LGPL 3, GPL 2, GPL 3 and other upstream license texts are in [`third_party_licenses/Qt-6.11.1/LICENSES`](third_party_licenses/Qt-6.11.1/LICENSES). [`qt-license-sources.json`](third_party_licenses/qt-license-sources.json) maps 96 official source entries from seven module families to 46 distinct retained texts, with Git blob hashes. Duplicated identical texts share one local file. The inclusion of a license text is not a declaration that every component using it is present in this binary.

Official references: [Qt licensing](https://doc.qt.io/qt-6.11/licensing.html), [Qt third-party code](https://doc.qt.io/qt-6.11/licenses-used-in-qt.html), [PySide source v6.11.1](https://github.com/pyside/pyside-setup/tree/v6.11.1). Online documentation may advance to a newer patch release; the copied license texts use the specific source tag.

## Included module families and third-party attributions

The portable build is intended to retain the Qt Core/Gui/Widgets/Network, Multimedia/MultimediaWidgets and Svg dependencies collected for the application. The final binary inventory, not this intended list, determines what was actually redistributed. QtDeclarative license texts are also supplied because dependency collection can include QML/Quick support.

- [`Qt-6.11.1/attributions`](third_party_licenses/Qt-6.11.1/attributions) contains 33 original attribution JSON files from direct `src/3rdparty` directories of QtBase, QtMultimedia and QtSvg at `v6.11.1`. They retain component names, copyright statements, license identifiers and source information. [`qt-attribution-sources.json`](third_party_licenses/qt-attribution-sources.json) records their source URLs and eight directories without a direct attribution file. This source set can include platform-specific components absent from the Windows bundle and does not constitute a build-specific SBOM.
- [`FFmpeg-Qt-6.11.1`](third_party_licenses/FFmpeg-Qt-6.11.1) contains the original Qt Multimedia FFmpeg attribution and eight accompanying license texts. The pinned Qt source identifies **FFmpeg 7.1.3**, plus its libjpeg, zlib and Boost attributions. The record describes the upstream Qt online-installer FFmpeg configuration; it does not independently prove the configure options of every DLL in a Python wheel. [Exact upstream record](https://github.com/qt/qtmultimedia/blob/v6.11.1/src/3rdparty/ffmpeg/qt_attribution.json).
- Upstream source and build references: [QtBase v6.11.1](https://github.com/qt/qtbase/tree/v6.11.1), [QtMultimedia v6.11.1](https://github.com/qt/qtmultimedia/tree/v6.11.1), [QtSvg v6.11.1](https://github.com/qt/qtsvg/tree/v6.11.1), [FFmpeg n7.1.3](https://github.com/FFmpeg/FFmpeg/tree/n7.1.3), [Qt FFmpeg build instructions](https://doc.qt.io/qt-6.11/qtmultimedia-building-ffmpeg.html).

## WebEngine is excluded from this portable alpha

The packaging decision for this alpha excludes QtWebEngineCore, QtWebEngineWidgets, QtWebEngineQuick and QtWebChannel, their process/resources, and disables the associated bundled mini-app functionality. The final bundle must be inspected for WebEngine DLLs, QtWebEngineProcess and WebEngine PAK files before this exclusion is claimed. Application source can retain optional integration code for future builds.

`QtWebEngine-6.11.1/CHROMIUM_LICENSE.txt` and `FFMPEG_LICENSE.md` are reference copies from the exact Chromium submodule selected by QtWebEngine `v6.11.1`: commit `58c11ad487f8a237cf0ac71cc3e818b52db150df`. They are **not** a complete Chromium third-party notice collection. An attempt to locate an aggregate credits document in the installed resource PAK did not find it; no partial extracted scripts are included. Reintroducing WebEngine requires its complete build-specific third-party notices and source/distribution review. [Pinned Chromium source](https://github.com/qt/qtwebengine-chromium/tree/58c11ad487f8a237cf0ac71cc3e818b52db150df).

## Distribution boundaries still requiring review

Keep these notices and all bundled dependency license files with the portable directory. Preserve the dynamically linked library layout and do not describe replacement/relinking rights as removed by the application license. The publisher must select and satisfy the applicable license terms, including source availability and replacement/relinking provisions where required. Source links alone are not asserted here to satisfy every corresponding-source or offer obligation.

This bounded collection did not generate a complete binary SBOM, prove the exact wheel build flags, gather every nested dependency-specific notice outside the retained attribution records, or audit codec patents. Python/non-Qt runtime package notices are collected separately by the packaging process. Missing coverage must remain visible in release review; these documents do not imply a commercial Qt license is required or that redistribution is automatically approved.
