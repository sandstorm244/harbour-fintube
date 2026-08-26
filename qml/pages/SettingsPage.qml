import QtQuick 2.0
import Sailfish.Silica 1.0

// App settings. Toggles are persisted by the Python backend, so they survive restarts and
// apply to every page. Room here for more options as they come up.
Page {
    id: page
    allowedOrientations: Orientation.All

    property string ytdlpStatus: ""

    Connections {
        target: app.backend
        onUpdateFinished: page.ytdlpStatus = message
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: "Settings" }

            // Setup first: without yt-dlp nothing plays, so it leads the panel.
            SectionHeader { text: "yt-dlp" }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.installing
                      ? ("Downloading yt-dlp… " + Math.round(app.backend.installPct) + "%")
                      : (app.backend.ready
                         ? ("Installed — version " + app.backend.ytdlpVersion)
                         : "Not found. Tap Download to fetch a self-contained copy into the "
                           + "app's own folder (kept independent of any system yt-dlp).")
                color: app.backend.ready ? Theme.secondaryHighlightColor : Theme.errorColor
                font.pixelSize: Theme.fontSizeSmall
            }

            Label {
                visible: page.ytdlpStatus.length > 0
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: page.ytdlpStatus
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            // Primary actions — at most two side by side so the labels never clip. The
            // left button is context-aware: Download when yt-dlp is missing, Update when
            // it's present (and it shows the % while a download runs, whatever started it).
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Theme.paddingMedium

                Button {
                    text: app.backend.installing
                          ? (Math.round(app.backend.installPct) + "%")
                          : (!app.backend.ready ? "Download"
                             : (app.backend.updating ? "Updating…" : "Update"))
                    enabled: !app.backend.installing && !app.backend.updating
                    onClicked: {
                        if (!app.backend.ready) {
                            page.ytdlpStatus = "Downloading yt-dlp…"
                            app.backend.installYtdlp()
                        } else {
                            page.ytdlpStatus = "Updating yt-dlp…"
                            app.backend.updateYtdlp()
                        }
                    }
                }
                Button {
                    text: "Recheck"
                    enabled: !app.backend.updating && !app.backend.installing
                    onClicked: app.backend.recheck()
                }
            }

            // Fetch a fresh copy — rarely needed, only if the installed binary breaks.
            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: app.backend.ready && !app.backend.installing
                enabled: !app.backend.updating
                text: "Reinstall"
                onClicked: {
                    page.ytdlpStatus = "Reinstalling yt-dlp…"
                    app.backend.installYtdlp()
                }
            }

            TextSwitch {
                visible: app.backend.ready
                text: "Use nightly builds"
                description: "Nightly yt-dlp ships fixes for YouTube breakages days sooner, at the "
                             + "cost of less testing. Applies on the next Update."
                automaticCheck: false
                checked: app.backend.ytdlpChannel === "nightly"
                onClicked: app.backend.setSetting("ytdlp_channel",
                             app.backend.ytdlpChannel === "nightly" ? "stable" : "nightly")
            }

            SectionHeader { text: "ffmpeg" }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.ffmpegInstalling
                      ? ("Downloading ffmpeg… " + Math.round(app.backend.ffmpegPct) + "%")
                      : (app.backend.ffmpegReady
                         ? ("Installed — " + app.backend.ffmpegVersion)
                         : "Optional. Lets downloads merge separate HD video + audio into one "
                           + "file; without it, video downloads fall back to 360p. Tap Download "
                           + "to fetch a static build into the app's folder.")
                color: app.backend.ffmpegReady ? Theme.secondaryHighlightColor : Theme.secondaryColor
                font.pixelSize: Theme.fontSizeSmall
            }

            Label {
                visible: app.backend.ffmpegStatusMsg.length > 0 && !app.backend.ffmpegInstalling
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.ffmpegStatusMsg
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Theme.paddingMedium

                Button {
                    text: app.backend.ffmpegInstalling
                          ? (Math.round(app.backend.ffmpegPct) + "%")
                          : (app.backend.ffmpegReady ? "Update" : "Download")
                    enabled: !app.backend.ffmpegInstalling
                    onClicked: app.backend.installFfmpeg()
                }
                Button {
                    text: "Recheck"
                    enabled: !app.backend.ffmpegInstalling
                    onClicked: app.backend.recheckFfmpeg()
                }
            }

            SectionHeader { text: "Content" }

            TextSwitch {
                text: "Hide Shorts"
                description: "Filter Shorts out of search results and channel video lists"
                automaticCheck: false
                checked: app.backend.hideShorts
                onClicked: app.backend.setHideShorts(!app.backend.hideShorts)
            }

            TextSwitch {
                text: "Skip sponsors"
                description: "Auto-skip SponsorBlock segments (sponsors, self-promo, reminders). "
                             + "Sends the video ID to sponsor.ajay.app."
                automaticCheck: false
                checked: app.backend.sponsorBlock
                onClicked: app.backend.setSponsorBlock(!app.backend.sponsorBlock)
            }

            SectionHeader { text: "Playback" }

            ComboBox {
                id: qualityCombo
                width: parent.width
                label: "Default quality"
                description: "Baseline video quality — the app plays this or the next lower one "
                             + "available. Decode is software: 1080p is 60fps and can tax the CPU, "
                             + "so 720p is the smooth default."
                // Index ↔ value map; "0" = best available (no cap).
                property var vals: ["1080", "720", "480", "360", "0"]
                currentIndex: Math.max(0, vals.indexOf(app.backend.defaultQuality))
                menu: ContextMenu {
                    MenuItem { text: "1080p" }
                    MenuItem { text: "720p" }
                    MenuItem { text: "480p" }
                    MenuItem { text: "360p" }
                    MenuItem { text: "Best available" }
                }
                onCurrentIndexChanged: {
                    var v = vals[currentIndex]
                    if (v !== app.backend.defaultQuality)
                        app.backend.setDefaultQuality(v)
                }
            }

            TextSwitch {
                text: "Hardware video decoding"
                description: "Experimental: decode video on the device's codec block instead "
                             + "of the CPU — lighter on the battery and smoother at 1080p/4K. "
                             + "Prefers VP9. Applies to the next video. If a video won't play, "
                             + "switch this off."
                automaticCheck: false
                checked: app.backend.hwDecode
                onClicked: app.backend.setHwDecode(!app.backend.hwDecode)
            }

            TextSwitch {
                text: "Keep display on while playing"
                description: "Stop the screen from dimming/blanking while a video is playing. "
                             + "Uses more battery, but also avoids the black-frame / garbled-text "
                             + "glitch some devices show when the display wakes mid-playback."
                automaticCheck: false
                checked: app.backend.keepDisplayOn
                onClicked: app.backend.setKeepDisplayOn(!app.backend.keepDisplayOn)
            }

            ValueButton {
                label: "Equalizer"
                value: app.backend.eqEnabled ? "On" : "Off"
                onClicked: pageStack.push(Qt.resolvedUrl("EqualizerPage.qml"))
            }

            Slider {
                id: boostSlider
                width: parent.width
                minimumValue: 100
                maximumValue: 500
                stepSize: 10
                value: Math.round(app.backend.boostGain * 100)
                label: "Volume boost"
                valueText: value + "%"
                // Live preview while dragging; persist + re-bind on release.
                onValueChanged: if (app.activePlayer) app.activePlayer.setBoost(value / 100)
                onReleased: {
                    app.backend.setBoostGain(value / 100)
                    value = Qt.binding(function() { return Math.round(app.backend.boostGain * 100) })
                }
            }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: "Pushes playback above the system maximum for quiet outputs (e.g. Bluetooth). "
                      + "A limiter tames the peaks so the extra loudness doesn't distort the way "
                      + "raising the system volume past 100% does. 100% = off."
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            ComboBox {
                id: clientCombo
                width: parent.width
                label: "Player client"
                description: "How yt-dlp identifies to YouTube. If a video won't play, try "
                             + "TV or iOS — several clients stream without a PO token."
                // Index ↔ value map; kept in sync with the menu below.
                property var vals: ["", "tv", "ios", "android_vr", "mweb", "web"]
                currentIndex: Math.max(0, vals.indexOf(app.backend.playerClient))
                menu: ContextMenu {
                    MenuItem { text: "Auto (yt-dlp default)" }
                    MenuItem { text: "TV" }
                    MenuItem { text: "iOS" }
                    MenuItem { text: "Android VR" }
                    MenuItem { text: "Mobile web" }
                    MenuItem { text: "Web" }
                }
                onCurrentIndexChanged: {
                    var v = vals[currentIndex]
                    if (v !== app.backend.playerClient)
                        app.backend.setSetting("player_client", v)
                }
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: "Advanced: a PO token proves the request came from a real browser. Paste "
                      + "one (with matching visitor data) only for stubborn videos that no "
                      + "client can play. Tokens expire after a few hours; leave blank otherwise."
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            TextField {
                width: parent.width
                label: "PO token"
                placeholderText: "e.g. web.gvs+MnQ…  (optional)"
                text: app.backend.poToken
                EnterKey.iconSource: "image://theme/icon-m-enter-accept"
                EnterKey.onClicked: focus = false
                onActiveFocusChanged: if (!activeFocus && text !== app.backend.poToken)
                                          app.backend.setSetting("po_token", text)
            }

            TextField {
                width: parent.width
                label: "Visitor data"
                placeholderText: "X-Goog-Visitor-Id  (optional)"
                text: app.backend.visitorData
                EnterKey.iconSource: "image://theme/icon-m-enter-accept"
                EnterKey.onClicked: focus = false
                onActiveFocusChanged: if (!activeFocus && text !== app.backend.visitorData)
                                          app.backend.setSetting("visitor_data", text)
            }

            SectionHeader { text: "PO token provider" }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: "YouTube now demands a fresh proof-of-origin token per video. This sets up "
                      + "the bgutil provider (v" + (app.backend.potTag || "…") + "): a small "
                      + "Deno helper that mints one automatically for each video, so you get full "
                      + "quality instead of 403s — no manual token juggling above.\n\n"
                      + "It runs sandboxed by Deno: network only, no file writes, no subprocesses, "
                      + "no native code, and it can't read anything outside its own folder. Opt-in — "
                      + "nothing is downloaded unless you tap Set up."
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.potInstalling
                      ? (app.backend.potStatusMsg || "Setting up…")
                      : (!app.backend.potDeno
                         ? "Deno runtime not found. Tap Download Deno below, or install it yourself."
                         : (app.backend.potInstalled
                            ? ("Installed" + (app.backend.potEnabled
                                 ? (app.backend.potRunning ? " · running" : " · on") : " · off"))
                            : "Not installed."))
                color: app.backend.potInstalled && app.backend.potEnabled
                       ? Theme.secondaryHighlightColor
                       : (!app.backend.potDeno ? Theme.errorColor : Theme.secondaryColor)
                font.pixelSize: Theme.fontSizeSmall
            }

            Label {
                visible: app.backend.potStatusMsg.length > 0 && !app.backend.potInstalling
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.potStatusMsg
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Button {
                visible: !app.backend.potDeno || app.backend.denoInstalling
                anchors.horizontalCenter: parent.horizontalCenter
                text: app.backend.denoInstalling
                      ? ("Downloading Deno… " + Math.round(app.backend.denoPct) + "%")
                      : "Download Deno"
                enabled: !app.backend.denoInstalling
                onClicked: app.backend.installDeno()
            }

            Label {
                visible: app.backend.denoStatusMsg.length > 0 && !app.backend.denoInstalling
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.denoStatusMsg
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            TextSwitch {
                visible: app.backend.potInstalled
                text: "Use PO token provider"
                description: "Fetch a per-video token so age/robot-gated videos return full quality."
                automaticCheck: false
                checked: app.backend.potEnabled
                onClicked: app.backend.setPotEnabled(!app.backend.potEnabled)
            }

            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: app.backend.potInstalling
                      ? "Setting up…"
                      : (app.backend.potInstalled ? "Reinstall provider" : "Set up provider")
                enabled: !app.backend.potInstalling && app.backend.potDeno
                onClicked: app.backend.installPotProvider()
            }

            Button {
                visible: app.backend.potInstalled
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Update to latest"
                enabled: !app.backend.potInstalling && app.backend.potDeno
                onClicked: app.backend.updatePotProvider()
            }
        }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: app.backend.updating || app.backend.installing || app.backend.potInstalling
                 || app.backend.ffmpegInstalling || app.backend.denoInstalling
    }
}
