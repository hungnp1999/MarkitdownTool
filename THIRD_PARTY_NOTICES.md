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