import QtQuick 2.0
import Sailfish.Silica 1.0
import "../logic.js" as Logic
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

            PageHeader { title: qsTr("Settings") }

            SectionHeader { text: qsTr("Content") }

            TextSwitch {
                text: qsTr("Hide Shorts")
                description: qsTr("Filter Shorts out of search results and channel video lists")
                automaticCheck: false
                checked: app.backend.hideShorts
                onClicked: app.backend.setHideShorts(!app.backend.hideShorts)
            }

            TextSwitch {
                text: qsTr("Hide watched videos")
                description: qsTr("Hide videos you've already watched from the subscription feed and "
                             + "channel video lists.")
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
                label: qsTr("Watch-history limit")
                valueText: qsTr("%n video(s)", "", value)
                onReleased: {
                    app.backend.setHistoryLimit(value)
                    value = Qt.binding(function() { return app.backend.historyLimit })
                }
            }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: qsTr("How many recently-watched videos the History page keeps — the oldest drop off "
                      + "once you pass this. (Resume points expire on their own after 30 days unused.)")
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            SectionHeader { text: qsTr("Playback") }

            ComboBox {
                id: qualityCombo
                width: parent.width
                label: qsTr("Default quality")
                description: qsTr("Baseline video quality — the app plays this or the next lower one "
                             + "available. Decode is software: 1080p is 60fps and can tax the CPU, "
                             + "so 720p is the smooth default.")
                // Index ↔ value map (Logic.QUALITY_VALUES, in the menu's order); "0" = best available.
                currentIndex: Logic.qualityIndex(app.backend.defaultQuality)
                menu: ContextMenu {
                    MenuItem { text: "1080p" }
                    MenuItem { text: "720p" }
                    MenuItem { text: "480p" }
                    MenuItem { text: "360p" }
                    MenuItem { text: qsTr("Best available") }
                }
                onCurrentIndexChanged: {
                    var v = Logic.qualityValue(currentIndex)
                    if (v !== app.backend.defaultQuality)
                        app.backend.setDefaultQuality(v)
                }
            }

            TextSwitch {
                text: qsTr("Hardware video decoding")
                description: qsTr("Experimental: decode video on the device's codec block instead "
                             + "of the CPU — lighter on the battery and smoother at 1080p/4K. "
                             + "Prefers VP9. Applies to the next video. If a video won't play, "
                             + "switch this off.")
                automaticCheck: false
                checked: app.backend.hwDecode
                onClicked: app.backend.setHwDecode(!app.backend.hwDecode)
            }

            TextSwitch {
                text: qsTr("Background audio")
                description: qsTr("Keep audio playing when you leave the video to browse elsewhere in "
                             + "the app. Control it from the cover and the lockscreen; reopen the "
                             + "video to pick playback back up where it is.")
                automaticCheck: false
                checked: app.backend.backgroundAudio
                onClicked: app.backend.setBackgroundAudio(!app.backend.backgroundAudio)
            }

            TextSwitch {
                text: qsTr("Portrait fullscreen")
                description: qsTr("Let the fullscreen button fill the screen while staying in portrait, "
                             + "instead of rotating to landscape. Tap the video's fullscreen "
                             + "control to toggle.")
                automaticCheck: false
                checked: app.backend.portraitFullscreen
                onClicked: app.backend.setPortraitFullscreen(!app.backend.portraitFullscreen)
            }

            ValueButton {
                label: qsTr("Equalizer")
                value: app.backend.eqEnabled ? qsTr("On") : qsTr("Off")
                onClicked: pageStack.push(Qt.resolvedUrl("EqualizerPage.qml"))
            }

            Slider {
                id: boostSlider
                width: parent.width
                minimumValue: 100
                maximumValue: 500
                stepSize: 10
                value: Math.round(app.backend.boostGain * 100)
                label: qsTr("Volume boost")
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
                text: qsTr("Pushes playback above the system maximum for quiet outputs (e.g. Bluetooth). "
                      + "A limiter tames the peaks so the extra loudness doesn't distort the way "
                      + "raising the system volume past 100% does. 100% = off.")
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            SectionHeader { text: qsTr("Downloads") }

            ValueButton {
                label: qsTr("Folder")
                value: app.backend.downloadDir ? app.backend.downloadDir : qsTr("App folder (default)")
                onClicked: pageStack.animatorPush(folderPickerPage)
            }

            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                text: page.dlError.length > 0
                      ? page.dlError
                      : (app.backend.downloadDir
                         ? qsTr("Videos are saved here. Tap Folder to change it.")
                         : qsTr("Videos are saved in the app's own private folder by default — pick a "
                           + "folder like Videos or an SD card to find them in the file manager."))
                color: page.dlError.length > 0 ? Theme.errorColor : Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }

            Button {
                visible: app.backend.downloadDir.length > 0
                anchors.horizontalCenter: parent.horizontalCenter
                text: qsTr("Reset to app folder")
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
                    page.dlError = (r && r.ok === false) ? (r.error || qsTr("Couldn't use that folder.")) : ""
                })
            }
        }
    }
}
