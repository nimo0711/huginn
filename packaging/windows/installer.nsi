Unicode True
!include "MUI2.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"
!define FORKRAIL_UNINSTALL "Software\Microsoft\Windows\CurrentVersion\Uninstall\Forkrail"
Name "Forkrail"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\Forkrail"
InstallDirRegKey HKCU "${FORKRAIL_UNINSTALL}" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "Forkrail"
VIAddVersionKey "FileDescription" "Forkrail Installer"
VIAddVersionKey "FileVersion" "${RELEASE_VERSION}"
VIAddVersionKey "LegalCopyright" "Forkrail contributors"
!define MUI_ICON "${SOURCE}\Forkrail.ico"
!define MUI_UNICON "${SOURCE}\Forkrail.ico"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Korean"
!insertmacro MUI_LANGUAGE "English"
Var Stage
Var Backup
Var InstallParent
Var ServiceBusy

!macro CHECK_CLOSED Prefix
Function ${Prefix}CheckClosed
  ; OPEN_EXISTING with no sharing checks the running image without modifying it.
  IfFileExists "$INSTDIR\Forkrail.exe" 0 service_check
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\Forkrail.exe", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .r0'
  IntCmp $0 -1 in_use
  System::Call 'kernel32::CloseHandle(p r0)'
  service_check:
  IfFileExists "$INSTDIR\service\forkrail-service.exe" 0 closed
  System::Call 'kernel32::CreateFileW(w "$INSTDIR\service\forkrail-service.exe", i 0x40000000, i 0, p 0, i 3, i 0, p 0) p .r0'
  IntCmp $0 -1 in_use
  System::Call 'kernel32::CloseHandle(p r0)'
  Goto closed
  in_use:
    IfSilent +2
    MessageBox MB_ICONSTOP "Close Forkrail and its service, then try again."
    SetErrorLevel 3
    Quit
  closed:
FunctionEnd
!macroend
!insertmacro CHECK_CLOSED ""
!insertmacro CHECK_CLOSED "un."

!macro INSTALL_LOCK Prefix
Function ${Prefix}LockInstaller
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Local\ForkrailInstaller") p .r0 ?e'
  Pop $1
  StrCmp $0 "0" lock_failed
  StrCmp $1 "183" lock_failed
  ; The process retains this handle until it exits.
  Return
  lock_failed:
    IfSilent +2
    MessageBox MB_ICONSTOP "Another Forkrail install or uninstall is in progress."
    SetErrorLevel 4
    Quit
FunctionEnd
!macroend
!insertmacro INSTALL_LOCK ""
!insertmacro INSTALL_LOCK "un."

Function .onInit
  Call LockInstaller
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Forkrail requires 64-bit Windows."
    Abort
  ${EndIf}
  SetShellVarContext current
  SetRegView 32
  ReadRegStr $0 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  StrCmp $0 "" check_user
  StrCmp $0 "0.0.0.0" check_user runtime_ready
  check_user:
  ReadRegStr $0 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  StrCmp $0 "" runtime_missing
  StrCmp $0 "0.0.0.0" runtime_missing runtime_ready
  runtime_missing:
    IfSilent +3
    MessageBox MB_OK "Microsoft WebView2 Runtime is required. Install it from the Microsoft page, then run this installer again."
    ExecShell "open" "https://developer.microsoft.com/microsoft-edge/webview2/"
    SetErrorLevel 2
    Abort
  runtime_ready:
FunctionEnd

Function un.onInit
  SetShellVarContext current
  SetRegView 32
  Call un.LockInstaller
FunctionEnd

!macro FRESH_SIBLING Result Failure
  ClearErrors
  GetTempFileName ${Result} "$InstallParent"
  IfErrors ${Failure}
  Delete "${Result}"
  IfErrors ${Failure}
!macroend

Function un.CheckServiceFile
  Push $0
  ; Test for sharing violations before changing any installed file.
  System::Call 'kernel32::CreateFileW(w "$R9", i 0x40010000, i 7, p 0, i 3, i 0, p 0) p .r0'
  IntCmp $0 -1 service_busy
  System::Call 'kernel32::CloseHandle(p r0)'
  Pop $0
  Push ""
  Return
  service_busy:
    Pop $0
    StrCpy $ServiceBusy "1"
    Push "StopLocate"
FunctionEnd

Section "Forkrail" SEC_MAIN
  ; Win32 resolves a new path too, whereas the NSIS path helper requires it to exist.
  System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r0, p 0) i .r1'
  IntCmp $1 0 invalid_directory
  IntCmp $1 ${NSIS_MAX_STRLEN} invalid_directory 0 invalid_directory
  StrCpy $INSTDIR $0
  StrCpy $0 $INSTDIR 1 -1
  StrCmp $0 '\' 0 +2
  StrCpy $INSTDIR $INSTDIR -1
  StrCmp $INSTDIR "$LOCALAPPDATA\Forkrail" invalid_directory
  ${DirState} "$INSTDIR" $0
  StrCmp $0 "1" 0 prepare
    IfFileExists "$INSTDIR\Forkrail.exe" 0 invalid_directory
    IfFileExists "$INSTDIR\Uninstall.exe" 0 invalid_directory
  prepare:
  Call CheckClosed
  ${GetParent} "$INSTDIR" $InstallParent
  StrCmp $InstallParent "" invalid_directory
  ClearErrors
  CreateDirectory "$InstallParent"
  IfErrors stage_failed
  !insertmacro FRESH_SIBLING $Stage stage_failed
  SetOutPath "$Stage"
  IfErrors stage_failed
  File /r "${SOURCE}\*"
  IfErrors stage_failed
  WriteUninstaller "$Stage\Uninstall.exe"
  IfErrors stage_failed
  ; Leave the previous installation intact until the new tree is complete.
  SetOutPath "$TEMP"
  IfErrors stage_failed
  StrCpy $Backup ""
  IfFileExists "$INSTDIR\*" 0 publish_stage
    !insertmacro FRESH_SIBLING $Backup stage_failed
    Rename "$INSTDIR" "$Backup"
    IfErrors stage_failed
  publish_stage:
  ClearErrors
  Rename "$Stage" "$INSTDIR"
  IfErrors rollback
  ClearErrors
  SetOutPath "$INSTDIR"
  CreateDirectory "$SMPROGRAMS\Forkrail"
  CreateShortcut "$SMPROGRAMS\Forkrail\Forkrail.lnk" "$INSTDIR\Forkrail.exe" "" "$INSTDIR\Forkrail.ico"
  WriteRegStr HKCU "${FORKRAIL_UNINSTALL}" "DisplayName" "Forkrail"
  WriteRegStr HKCU "${FORKRAIL_UNINSTALL}" "DisplayVersion" "${RELEASE_VERSION}"
  WriteRegStr HKCU "${FORKRAIL_UNINSTALL}" "Publisher" "Forkrail contributors"
  WriteRegStr HKCU "${FORKRAIL_UNINSTALL}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${FORKRAIL_UNINSTALL}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "${FORKRAIL_UNINSTALL}" "DisplayIcon" "$INSTDIR\Forkrail.exe"
  WriteRegDWORD HKCU "${FORKRAIL_UNINSTALL}" "NoModify" 1
  WriteRegDWORD HKCU "${FORKRAIL_UNINSTALL}" "NoRepair" 1
  IfErrors registration_failed
  StrCmp $Backup "" done
  ; Clean only application-owned paths; leave unrelated files in the backup.
  Delete "$Backup\Forkrail.exe"
  Delete "$Backup\Forkrail.ico"
  Delete "$Backup\Uninstall.exe"
  RMDir /r "$Backup\service"
  RMDir "$Backup"
  Goto done
  rollback:
    StrCmp $Backup "" stage_failed
    ClearErrors
    Rename "$Backup" "$INSTDIR"
    IfErrors rollback_failed
  stage_failed:
    SetOutPath "$TEMP"
    StrCmp $Stage "" stage_message
    Delete "$Stage"
    RMDir /r "$Stage"
  stage_message:
    IfSilent +2
    MessageBox MB_ICONSTOP "Forkrail could not be installed. The previous version was preserved. Check file permissions and try again."
    SetErrorLevel 5
    Quit
  rollback_failed:
    RMDir /r "$Stage"
    DetailPrint "The previous installation is available at $Backup"
    IfSilent +2
    MessageBox MB_ICONSTOP "Forkrail could not be installed. The previous installation is available at $Backup"
    SetErrorLevel 7
    Quit
  registration_failed:
    IfSilent +2
    MessageBox MB_ICONSTOP "The application was copied to $INSTDIR but Windows registration could not be completed."
    SetErrorLevel 7
    Quit
  invalid_directory:
    IfSilent +2
    MessageBox MB_ICONSTOP "Choose an empty folder or an existing Forkrail installation. The Forkrail data directory cannot be used."
    SetErrorLevel 5
    Quit
  done:
SectionEnd

Section "Uninstall"
  Call un.CheckClosed
  ; _?= is also accepted by NSIS. Only remove an actual installation.
  ReadRegStr $0 HKCU "${FORKRAIL_UNINSTALL}" "InstallLocation"
  StrCmp $0 $INSTDIR 0 uninstall_failed
  IfFileExists "$INSTDIR\service\*.*" 0 detach
  StrCpy $ServiceBusy "0"
  ${un.Locate} "$INSTDIR\service" "/L=F" "un.CheckServiceFile"
  IfErrors uninstall_failed
  StrCmp $ServiceBusy "1" uninstall_failed
  detach:
  ; Move the entire application aside before deleting parts of it. A lock or
  ; permission error during the move leaves the installation and registry alone.
  SetOutPath "$TEMP"
  IfErrors uninstall_failed
  ${un.GetParent} "$INSTDIR" $InstallParent
  StrCmp $InstallParent "" uninstall_failed
  !insertmacro FRESH_SIBLING $Stage uninstall_failed
  Rename "$INSTDIR" "$Stage"
  IfErrors uninstall_failed
  ClearErrors
  Delete "$Stage\Forkrail.exe"
  Delete "$Stage\Forkrail.ico"
  Delete "$Stage\Uninstall.exe"
  RMDir /r "$Stage\service"
  StrCpy $1 ""
  IfErrors uninstall_cleanup_failed
  ; Keep unrelated contents if the user put files in the application folder.
  RMDir "$Stage"
  Goto remove_registration
  uninstall_cleanup_failed:
    StrCpy $1 "Remaining application files are available at $Stage."
  remove_registration:
    Delete "$SMPROGRAMS\Forkrail\Forkrail.lnk"
    RMDir "$SMPROGRAMS\Forkrail"
    ClearErrors
    DeleteRegKey HKCU "${FORKRAIL_UNINSTALL}"
    IfErrors uninstall_unregister_failed
    StrCmp $1 "" uninstall_done
    Goto uninstall_incomplete
  uninstall_unregister_failed:
    StrCpy $1 "Windows could not remove the Forkrail uninstall entry. $1"
  uninstall_incomplete:
    DetailPrint "$1"
    IfSilent +2
    MessageBox MB_ICONSTOP "$1"
    SetErrorLevel 7
    Quit
  uninstall_failed:
    IfSilent +2
    MessageBox MB_ICONSTOP "Forkrail could not be uninstalled. Close Forkrail and retry. Your installation and uninstall entry are preserved."
    SetErrorLevel 6
    Quit
  uninstall_done:
SectionEnd
