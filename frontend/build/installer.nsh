; electron-builder NSIS customization.
;
; The default assisted installer opens straight on the directory page, which
; means installerSidebar.bmp only ever appears on the finish page. Adding the
; welcome page puts it at the front too, so the wizard reads as one piece.
;
; The welcome and finish pages carry their own text instead of the NSIS
; language-file defaults, because those defaults talk about "the setup wizard"
; and the finish title reads as an instruction rather than a result. The text
; lives in LangStrings so English users still get English: every compiled
; language is covered, with English as the text for the ones we don't translate.
;
; LangStrings must be declared after the MUI_LANGUAGE inserts, and
; electron-builder includes this file before them, so the declarations go in
; customHeader (inserted after addLangs) rather than at the top level here.
;
; The uninstaller already gets MUI_UNPAGE_WELCOME by default, so
; uninstallerSidebar.bmp needs no hook of its own.

!macro LOCWARP_STRINGS_EN LANGID
  LangString LOCWARP_WELCOME_TITLE ${LANGID} "Welcome to LocWarp"
  LangString LOCWARP_WELCOME_TEXT ${LANGID} "LocWarp is a free, open-source virtual location tool for iOS.$\r$\n$\r$\nConnect your iPhone over USB or Wi-Fi, then pick a spot on the map, walk a route, or save the places you use often. No jailbreak and no account needed.$\r$\n$\r$\nInstalling requires administrator rights. Click Next to continue."
  LangString LOCWARP_FINISH_TITLE ${LANGID} "Installation complete"
  LangString LOCWARP_FINISH_TEXT ${LANGID} "LocWarp has been installed on this computer.$\r$\n$\r$\nBefore the first run, turn on Developer Mode on your iPhone and connect it over USB."
!macroend

!macro LOCWARP_STRINGS_ZH_TW LANGID
  LangString LOCWARP_WELCOME_TITLE ${LANGID} "歡迎使用 LocWarp"
  LangString LOCWARP_WELCOME_TEXT ${LANGID} "LocWarp 是一套免費、開放原始碼的 iOS 模擬定位工具。$\r$\n$\r$\n用 USB 或 Wi-Fi 連上 iPhone 後,就能在地圖上指定位置、沿路徑移動,或把常用地點存成座標。不必越獄,也不需要註冊帳號。$\r$\n$\r$\n安裝過程需要系統管理員權限。按「下一步」繼續。"
  LangString LOCWARP_FINISH_TITLE ${LANGID} "安裝完成"
  LangString LOCWARP_FINISH_TEXT ${LANGID} "LocWarp 已經安裝到這台電腦。$\r$\n$\r$\n第一次使用前,請先在 iPhone 上開啟開發者模式,並用 USB 連接電腦。"
!macroend

!macro LOCWARP_STRINGS_ZH_CN LANGID
  LangString LOCWARP_WELCOME_TITLE ${LANGID} "欢迎使用 LocWarp"
  LangString LOCWARP_WELCOME_TEXT ${LANGID} "LocWarp 是一款免费、开源的 iOS 虚拟定位工具。$\r$\n$\r$\n用 USB 或 Wi-Fi 连上 iPhone 后,就能在地图上指定位置、沿路径移动,或把常用地点存成坐标。无需越狱,也不用注册账号。$\r$\n$\r$\n安装过程需要管理员权限。点击「下一步」继续。"
  LangString LOCWARP_FINISH_TITLE ${LANGID} "安装完成"
  LangString LOCWARP_FINISH_TEXT ${LANGID} "LocWarp 已经安装到这台电脑。$\r$\n$\r$\n第一次使用前,请先在 iPhone 上开启开发者模式,并用 USB 连接电脑。"
!macroend

!macro customHeader
  !insertmacro LOCWARP_STRINGS_ZH_TW ${LANG_TRADCHINESE}
  !insertmacro LOCWARP_STRINGS_ZH_CN ${LANG_SIMPCHINESE}

  !insertmacro LOCWARP_STRINGS_EN ${LANG_ENGLISH}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_GERMAN}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_FRENCH}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_SPANISHINTERNATIONAL}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_JAPANESE}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_KOREAN}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_ITALIAN}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_DUTCH}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_DANISH}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_SWEDISH}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_NORWEGIAN}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_FINNISH}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_RUSSIAN}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_PORTUGUESE}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_PORTUGUESEBR}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_POLISH}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_UKRAINIAN}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_CZECH}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_SLOVAK}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_HUNGARIAN}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_ARABIC}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_TURKISH}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_THAI}
  !insertmacro LOCWARP_STRINGS_EN ${LANG_VIETNAMESE}
!macroend

!macro customWelcomePage
  !define MUI_WELCOMEPAGE_TITLE "$(LOCWARP_WELCOME_TITLE)"
  !define MUI_WELCOMEPAGE_TEXT "$(LOCWARP_WELCOME_TEXT)"
  !insertmacro MUI_PAGE_WELCOME
!macroend

; Replaces electron-builder's default finish page. The run-after-install
; checkbox is kept, so StartApp is copied from assistedInstaller.nsh.
!macro customFinishPage
  Function StartApp
    ${if} ${isUpdated}
      StrCpy $1 "--updated"
    ${else}
      StrCpy $1 ""
    ${endif}
    ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "$1"
  FunctionEnd

  !define MUI_FINISHPAGE_RUN
  !define MUI_FINISHPAGE_RUN_FUNCTION "StartApp"
  !define MUI_FINISHPAGE_TITLE "$(LOCWARP_FINISH_TITLE)"
  !define MUI_FINISHPAGE_TEXT "$(LOCWARP_FINISH_TEXT)"
  !insertmacro MUI_PAGE_FINISH
!macroend
