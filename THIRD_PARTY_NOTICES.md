# Third-Party Notices

MarkItDownTool makes use of third-party open-source software and libraries.

Ownership, copyrights, trademarks, and licenses remain the property of their respective owners.

The following list may not be exhaustive and may change between releases.

---

## Microsoft MarkItDown

License: MIT License

Copyright belongs to Microsoft and contributors.

Repository:
https://github.com/microsoft/markitdown

---

## pdfplumber

License: MIT License

Repository:
https://github.com/jsvine/pdfplumber

## pdfminer.six

License: MIT License

Repository:
https://github.com/pdfminer/pdfminer.six

pdfplumber uses pdfminer.six for PDF parsing. Microsoft MarkItDown uses these
packages in its PDF conversion path.

## xlrd

License: BSD License

Repository:
https://github.com/python-excel/xlrd

Microsoft MarkItDown uses xlrd to read legacy Excel (`.xls`) workbooks. xlrd only reads cell data; it does not run macros.

## pywin32

License: Python Software Foundation License (PSF-2.0)

Repository:
https://github.com/mhammond/pywin32

MarkitdownTool uses pywin32 to automate an already installed Microsoft Word, PowerPoint, or Excel, which converts legacy Office and OpenDocument files (`.doc`, `.rtf`, `.odt`, `.ppt`, `.odp`, `.ods`). Microsoft Office is not distributed with MarkitdownTool; it is licensed separately by Microsoft.

## Pillow

License: MIT-CMU License (HPND)

Repository:
https://github.com/python-pillow/Pillow

MarkitdownTool uses Pillow to read image resolution and to flatten transparent images before OCR.

## pypdfium2 and PDFium

pypdfium2 Python bindings: Apache-2.0 or BSD-3-Clause.

PDFium: BSD-style license. PDFium also includes third-party components under
their respective licenses. The license files for the pypdfium2/PDFium wheel
used by this release are installed under `licenses/pypdfium2/`.

Repositories:
https://github.com/pypdfium2-team/pypdfium2
https://pdfium.googlesource.com/pdfium/

MarkItDownTool uses pypdfium2 for fast PDF text-layer detection.

---

## OCRmyPDF

License: MPL 2.0

Copyright belongs to OCRmyPDF contributors.

Repository:
https://github.com/ocrmypdf/OCRmyPDF

---

## Tesseract OCR

License: Apache License 2.0

Copyright belongs to The Tesseract OCR Project.

Repository:
https://github.com/tesseract-ocr/tesseract

---

## pikepdf and QPDF

pikepdf: MPL 2.0. QPDF: Apache License 2.0.

Repositories:
https://github.com/pikepdf/pikepdf
https://github.com/qpdf/qpdf

OCRmyPDF uses pikepdf, which includes the QPDF library, to read and write PDF files.

## fpdf2

License: GNU Lesser General Public License v3.0 (LGPL-3.0-only)

Repository:
https://github.com/py-pdf/fpdf2

## img2pdf

License: GNU Lesser General Public License v3.0 (LGPL-3.0-or-later)

Repository:
https://gitlab.mister-muffin.de/josch/img2pdf

OCRmyPDF uses fpdf2 to render the OCR text layer and img2pdf to turn images into PDF pages. Both are included unmodified as Python modules inside `markitdown_gui.exe` and `markitdowntool.exe`. The texts of the GNU LGPL v3 and the GNU GPL v3 it builds on are included as `licenses/LGPL-3.0.txt` and `licenses/GPL-3.0.txt` (in the installation folder, and inside each executable).

You may replace these libraries with your own versions. The complete source code of MarkitdownTool and the build script are available at https://github.com/hungnp1999/MarkitdownTool: install your version of the library with `pip` and run MarkitdownTool from source, or rebuild the executables with `markitdown_gui.spec`. The source code of each library is available from its repository above.

## certifi

License: MPL 2.0

Repository:
https://github.com/certifi/python-certifi

## PyInstaller

License: GNU General Public License v2.0 with the PyInstaller bootloader exception

Repository:
https://github.com/pyinstaller/pyinstaller

The executables are built with PyInstaller. Its bootloader exception allows distributing the built executables under any license.

---

## Python

License: Python Software Foundation License

Website:
https://www.python.org

---

## Additional Components

Additional dependencies may be included through the Python package ecosystem.

Users should review the corresponding license terms of those dependencies when redistributing the software.

---

## Disclaimer

This document is provided for informational purposes only and does not replace the original licenses of third-party projects.

Users must consult the original projects for authoritative licensing information.