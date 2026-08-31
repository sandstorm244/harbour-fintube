import QtQuick 2.0
import Sailfish.Silica 1.0
import Sailfish.Pickers 1.0

// App settings — playback and content preferences. Third-party tool management (yt-dlp, ffmpeg,
// PO-token provider) lives on its own Providers page, reached from Home → More → Providers.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool hideDock: true   // hide the now-playing dock / resume bar over Settings
    property string dlError: ""   // last download-folder error (e.g. not writable), shown inline

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: "Settings" }

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

            TextSwitch {
                text: "Hide watched videos"
                description: "Hide videos you've already watched from the subscription feed and "
                             + "channel video lists."
                automaticCheck: false
                checked: app.backend.hideWatched
                onClicked: app.backend.setHideWatched(!app.backend.hideWatched)
            }

            Slider {
                id: historySlider
                width: parent.width
                minimumValue: 100
                maximumValue: 2000
                stepSize: 100
                value: app.backend.historyLimit
                label: "Watch-history limit"
                valueText: value + " videos"
                onReleased: {
                    app.backend.setHistoryLimit(value)
                    value = Qt.binding(function() { return app.backend.historyLimit })
                }
            }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: "How many recently-watched videos the History page keeps — the oldest drop off "
                      + "once you pass this. (Resume points expire on their own after 30 days unused.)"
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
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

            TextSwitch {
                text: "Portrait fullscreen"
                description: "Let the fullscreen button fill the screen while staying in portrait, "
                             + "instead of rotating to landscape. Tap the video's fullscreen "
                             + "control to toggle."
                automaticCheck: false
                checked: app.backend.portraitFullscreen
                onClicked: app.backend.setPortraitFullscreen(!app.backend.portraitFullscreen)
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

            SectionHeader { text: "Downloads" }

            ValueButton {
                label: "Folder"
                value: app.backend.downloadDir ? app.backend.downloadDir : "App folder (default)"
                onClicked: pageStack.animatorPush(folderPickerPage)
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: page.dlError.length > 0
                      ? page.dlError
                      : (app.backend.downloadDir
                         ? "Videos are saved here. Tap Folder to change it."
                         : "Videos are saved in the app's own private folder by default — pick a "
                           + "folder like Videos or an SD card to find them in the file manager.")
                color: page.dlError.length > 0 ? Theme.errorColor : Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Button {
                visible: app.backend.downloadDir.length > 0
                anchors.horizontalCenter: parent.horizontalCenter
                text: "Reset to app folder"
                onClicked: app.backend.setDownloadDir("", function(r) { page.dlError = "" })
            }
        }
    }

    // Folder picker for the download location. FinTube runs unsandboxed, so it can browse and
    // write anywhere in the home tree; Python validates the pick is writable before saving.
    Component {
        id: folderPickerPage
        FolderPickerPage {
            onSelectedPathChanged: {
                app.backend.setDownloadDir(selectedPath, function(r) {
                    page.dlError = (r && r.ok === false) ? (r.error || "Couldn't use that folder.") : ""
                })
            }
        }
    }
}
