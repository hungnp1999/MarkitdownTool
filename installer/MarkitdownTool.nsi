!include "MUI2.nsh"

!define ROOT_DIR "${__FILEDIR__}\.."
!define PRODUCT_NAME "MarkitdownTool"
!define PRODUCT_VERSION "2.3.0"
!define PRODUCT_APP_ID "{A63D865D-254C-4A4F-9FC8-0A7FD2D2B146}"
!define PRODUCT_KEY "Software\MarkitdownTool"
!define LEGACY_UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\MarkitdownTool"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_APP_ID}"

Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "${ROOT_DIR}\dist\MarkitdownTool-Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\${PRODUCT_NAME}"
InstallDirRegKey HKCU "${PRODUCT_KEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetOverwrite on
VIProductVersion "2.3.0.0"
VIAddVersionKey "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey "ProductVersion" "${PRODUCT_VERSION}"
VIAddVersionKey "FileDescription" "${PRODUCT_NAME} installer"
VIAddVersionKey "FileVersion" "${PRODUCT_VERSION}"
VIAddVersionKey "LegalCopyright" "Copyright (c) 2026 Nguyen Phu Hung"

Var DataDir

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_COMPONENTS
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function .onInit
  ReadRegStr $0 HKCU "${LEGACY_UNINSTALL_KEY}" "UninstallString"
  StrCmp $0 "" legacy_done
  ReadRegStr $1 HKCU "${PRODUCT_KEY}" "InstallDir"
  StrCmp $1 "" 0 legacy_done
  StrCpy $0 $0 -15 1
  IfFileExists "$0\uninstall.exe" 0 legacy_done
  StrCpy $INSTDIR $0
legacy_done:
  ReadRegStr $DataDir HKCU "${PRODUCT_KEY}" "DataDir"
  StrCmp $DataDir "" 0 data_done
  StrCpy $DataDir "$LOCALAPPDATA\${PRODUCT_NAME}"
data_done:
FunctionEnd

Function un.onInit
  ReadRegStr $DataDir HKCU "${PRODUCT_KEY}" "DataDir"
  StrCmp $DataDir "" 0 un_data_done
  StrCpy $DataDir "$LOCALAPPDATA\${PRODUCT_NAME}"
un_data_done:
FunctionEnd

Section "${PRODUCT_NAME} application" MainSection
  SectionIn RO
  SetOutPath "$INSTDIR"
  File "${ROOT_DIR}\dist\markitdown_gui.exe"
  File "${ROOT_DIR}\dist\markitdowntool.exe"
  File "${ROOT_DIR}\LICENSE.md"
  File "${ROOT_DIR}\DISCLAIMER.md"
  File "${ROOT_DIR}\PRIVACY.md"
  File "${ROOT_DIR}\THIRD_PARTY_NOTICES.md"
  File "${ROOT_DIR}\README.md"

  SetOutPath "$INSTDIR\licenses\pypdfium2"
  File /r "${ROOT_DIR}\build\installer_payload\pypdfium2_licenses\*"

  CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
  CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\markitdown_gui.exe" "" "$INSTDIR\markitdown_gui.exe" 0 SW_SHOWNORMAL "" "$INSTDIR"

  CreateDirectory "$DataDir"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${PRODUCT_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_KEY}" "DataDir" "$DataDir"
  DeleteRegKey HKCU "${LEGACY_UNINSTALL_KEY}"

  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "Nguyen Phu Hung"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\markitdown_gui.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Bundled Tesseract OCR runtime" TesseractSection
  SetOutPath "$INSTDIR\tesseract"
  File /r "${ROOT_DIR}\build\installer_payload\tesseract\*"
SectionEnd

Section /o "Desktop shortcut" DesktopShortcut
  CreateShortcut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\markitdown_gui.exe" "" "$INSTDIR\markitdown_gui.exe" 0 SW_SHOWNORMAL "" "$INSTDIR"
SectionEnd

Section /o un.RemoveLanguagePackages "Remove downloaded OCR language packages"
  RMDir /r "$DataDir\tesseract\tessdata"
  RMDir "$DataDir\tesseract"
SectionEnd

Section /o un.RemoveUserData "Remove user settings, logs, and output"
  RMDir /r "$DataDir\output"
  Delete "$LOCALAPPDATA\${PRODUCT_NAME}\config.json"
  RMDir "$LOCALAPPDATA\${PRODUCT_NAME}"
  RMDir "$DataDir"
  DeleteRegValue HKCU "${PRODUCT_KEY}" "DataDir"
SectionEnd

Section un.RemoveTesseract "Remove bundled Tesseract OCR runtime"
  RMDir /r "$INSTDIR\tesseract"
SectionEnd

Section un.RemoveApplication "Remove ${PRODUCT_NAME} application"
  SectionIn RO
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
  RMDir "$SMPROGRAMS\${PRODUCT_NAME}"
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"
  Delete "$INSTDIR\markitdown_gui.exe"
  Delete "$INSTDIR\markitdowntool.exe"
  Delete "$INSTDIR\LICENSE.md"
  Delete "$INSTDIR\DISCLAIMER.md"
  Delete "$INSTDIR\PRIVACY.md"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.md"
  Delete "$INSTDIR\README.md"
  RMDir /r "$INSTDIR\licenses\pypdfium2"
  RMDir "$INSTDIR\licenses"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegValue HKCU "${PRODUCT_KEY}" "InstallDir"
SectionEnd