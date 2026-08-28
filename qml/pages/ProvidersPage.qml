import QtQuick 2.0
import Sailfish.Silica 1.0

// Providers — the third-party tools the app manages for you: yt-dlp (the extractor), ffmpeg (HD
// download merging) and the optional PO-token provider (Deno sidecar) that unlocks full quality.
// Split out of Settings to keep that panel focused on playback. Reached from Home → More → Providers.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool hideDock: true   // hide the now-playing dock / resume bar over Providers

    // Re-read provider status each time the page is shown. prewarm() brings the server up on a
    // background thread ~after launch, so the value cached at startup can be stale ("server not
    // started") by the time you open this page; refreshing here keeps the status line honest.
    onStatusChanged: if (status === PageStatus.Active) app.backend.loadPotStatus()

    property string ytdlpStatus: ""

    Connections {
        target: app.backend
        onUpdateFinished: page.ytdlpStatus = message
    }

    // PO-provider status line — kept as functions so the many states stay readable. Both read the
    // backend properties directly, so QML re-binds them whenever the status refreshes.
    function potStateText() {
        var b = app.backend
        if (b.potInstalling) return b.potStatusMsg || "Setting up…"
        if (!b.potDeno) return "Deno runtime not found. Tap Download Deno below."
        if (!b.potInstalled) return "Provider not installed."
        if (!b.potEnabled) return "Installed · off"
        if (b.potResponding)
            return "Installed · working" + (b.potServerVersion ? " (v" + b.potServerVersion + ")" : "")
        if (b.potRunning) return "Installed · on — server not answering yet"
        return "Installed · on — server not started"
    }
    function potStateColor() {
        var b = app.backend
        if (b.potInstalling) return Theme.secondaryColor
        if (!b.potDeno) return Theme.errorColor
        if (b.potEnabled && b.potResponding) return Theme.secondaryHighlightColor
        if (b.potEnabled && !b.potResponding) return Theme.errorColor
        return Theme.secondaryColor
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: "Providers" }

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

            SectionHeader { text: "PO token provider" }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: "YouTube now demands a fresh proof-of-origin token per video. This sets up "
                      + "the bgutil provider (v" + (app.backend.potTag || "…") + "): a small "
                      + "Deno helper that mints one automatically for each video, so you get full "
                      + "quality instead of 403s.\n\n"
                      + "It runs sandboxed by Deno: network only, no file writes, no subprocesses, "
                      + "no native code, and it can't read anything outside its own folder. Opt-in — "
                      + "nothing is downloaded unless you tap Set up."
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            // Status line. "running" means the port is open; "working" is the stronger signal that
            // the server actually answered an HTTP probe — that's the state that unlocks full quality.
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: page.potStateText()
                color: page.potStateColor()
                font.pixelSize: Theme.fontSizeSmall
            }

            // The reason it isn't working, when the app knows one (Deno missing, server crashed,
            // clone/deps failed…). Hidden when the provider is healthy.
            Label {
                visible: app.backend.potLastError.length > 0 && !app.backend.potInstalling
                         && !(app.backend.potEnabled && app.backend.potResponding)
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: app.backend.potLastError
                color: Theme.errorColor
                font.pixelSize: Theme.fontSizeExtraSmall
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

            // Confirm WHICH Deno is in use — the launcher trims PATH, so this reassures the user
            // that their ~/.local/bin (or the app-managed) Deno was actually found.
            Label {
                visible: app.backend.potDeno && app.backend.potDenoPath.length > 0
                         && !app.backend.denoInstalling
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: "Deno found: " + app.backend.potDenoPath
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

            SectionHeader { text: "Diagnostics" }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: "A full report of what the app can see: which binaries were found, whether the "
                      + "token server is alive and answering, and the tail of its log. Copy it if you "
                      + "need help getting the provider working."
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Button {
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Run diagnostics"
                onClicked: {
                    diagArea.text = "Running…"
                    app.backend.potDiagnostics(function(res) {
                        diagArea.text = (res && res.report) ? res.report : "No report."
                    })
                }
            }

            TextArea {
                id: diagArea
                visible: text.length > 0
                width: parent.width
                readOnly: true
                label: "Provider report"
                font.pixelSize: Theme.fontSizeExtraSmall
                font.family: "monospace"
                text: ""
            }

            Button {
                visible: diagArea.text.length > 0 && diagArea.text !== "Running…"
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Copy report"
                onClicked: Clipboard.text = diagArea.text
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
