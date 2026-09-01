import QtQuick 2.0
import Sailfish.Silica 1.0

// Player controls overlaid on the video, YouTube-style: a large centred play/pause and a
// thin scrubber pinned to the bottom edge, so the video stays visible instead of being
// covered by a tall bar. Fills the video surface; the page owns playback state and reacts
// to the signals. Shown/hidden (and faded) by the parent.
Item {
    id: root

    property int positionMs: 0
    property int durationMs: 0
    property bool isPlaying: false
    property real playbackRate: 1.0
    property bool speedEnabled: true
    property var chapters: []          // [{start,title}] seconds — ticks on the scrubber
    property string currentChapter: ""
    property bool fullscreen: false    // true when the page is in forced-landscape mode
    property var qualities: []         // [{itag,label,video_url}] highest-first
    property string currentQuality: "" // label of the track playing now
    property bool qualityEnabled: true // only when the switchable (GStreamer/DASH) path is live

    // ALL playback settings collapsed behind ONE ⚙ gear — Subtitles, Quality, Speed (Audio later) —
    // so nothing crowds the overlay or the SFOS back-gesture corner. A multi-level menu: a main
    // list, a per-setting drill-in, and (for captions) a further Translate drill-in.
    property var speeds: [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
    property bool playbackMenuOpen: false // parent reads this to hold the controls open
    property string playbackSection: ""   // "" main | "captions" | "translate" | "quality" | "speed"

    // Captions. tracks = the short real list (Off + manual + one ASR/lang); translations = the
    // big auto-translate set, demoted behind the "Translate…" drill-in so the menu never explodes.
    property var captionTracks: []          // [{lang,name,kind,url}]
    property var captionTranslations: []    // [{lang,name,url}]
    property string currentCaptionLang: ""  // lang of the active track ("" = off)
    property string captionFilter: ""       // live filter over the translate list

    // Audio (dub) tracks — only present on multi-language videos. One entry per language.
    property var audioTracks: []            // [{lang,name,is_original,itag,audio_url}]
    property string currentAudioItag: ""    // itag of the audio track playing now
    property bool audioEnabled: true        // only in dual-source mode (swappable audio branch)

    signal seekRequested(int ms)
    signal togglePlay()
    signal speedSelected(real rate)
    signal qualitySelected(var q)
    // Chosen audio track: an entry object {lang,name,itag,audio_url,...}.
    signal audioSelected(var a)
    // Chosen caption: a track object {lang,name,url,...}, or null for "Off".
    signal captionChosen(var track)
    signal toggleFullscreen()
    signal interacted()

    property bool captionAvailable: captionTracks.length > 0 || captionTranslations.length > 0
    // Only offer the Audio row when the video has a language CHOICE (dubs) AND we're in the
    // dual-source path whose audio branch can actually be swapped (not a muxed/HLS fallback).
    property bool audioAvailable: audioEnabled && audioTracks.length > 1

    // Display name of the audio track currently playing (matched by itag), for the Audio row.
    function currentAudioName() {
        for (var i = 0; i < root.audioTracks.length; i++)
            if (root.audioTracks[i].itag === root.currentAudioItag)
                return root.audioTracks[i].name
        return ""
    }
    onPlaybackMenuOpenChanged: if (!playbackMenuOpen) root.playbackSection = ""   // reopen on main
    // Off the translate level, reset its filter + field so the search always reopens empty and in
    // sync with the (cleared) filter.
    onPlaybackSectionChanged: if (playbackSection !== "translate") {
        root.captionFilter = ""
        captionSearch.text = ""
    }

    // Translations matching the live filter (by display name or language code). Referenced from
    // the Repeater's model so it re-runs whenever the filter or the translation set changes.
    function filteredTranslations() {
        var f = root.captionFilter.toLowerCase()
        if (f === "")
            return root.captionTranslations
        var out = []
        for (var i = 0; i < root.captionTranslations.length; i++) {
            var t = root.captionTranslations[i]
            if ((t.name || "").toLowerCase().indexOf(f) >= 0
                    || (t.lang || "").toLowerCase().indexOf(f) >= 0)
                out.push(t)
        }
        return out
    }

    // A Canvas drops its buffer when the app is backgrounded, so the drawn glyphs come back
    // blank. Repaint them when the app returns to the foreground and when the controls are
    // re-shown (the parent toggles `enabled`).
    onEnabledChanged: if (enabled) { glyph.requestPaint(); fsGlyph.requestPaint(); gearGlyph.requestPaint() }
    Connections {
        target: Qt.application
        onActiveChanged: if (Qt.application.active) {
            glyph.requestPaint()
            fsGlyph.requestPaint()
            gearGlyph.requestPaint()
        }
    }

    // --- centred play / pause: the symbol is DRAWN, not a theme icon. The theme
    // play/pause icons are a glyph baked onto a grey disc, so we paint white bars / a
    // triangle ourselves on a single dark undercircle. ---
    Rectangle {
        id: playDisc
        anchors.centerIn: parent
        width: Theme.itemSizeMedium
        height: width
        radius: width / 2
        color: '#00000000'

        Canvas {
            id: glyph
            anchors.centerIn: parent
            width: parent.width * 0.36
            height: width
            property bool playing: root.isPlaying
            onPlayingChanged: requestPaint()
            onWidthChanged: requestPaint()
            Component.onCompleted: requestPaint()
            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                ctx.fillStyle = "white"
                if (playing) {                       // pause: two bars
                    var bw = width * 0.32
                    ctx.fillRect(0, 0, bw, height)
                    ctx.fillRect(width - bw, 0, bw, height)
                } else {                             // play: right-pointing triangle
                    ctx.beginPath()
                    ctx.moveTo(0, 0)
                    ctx.lineTo(0, height)
                    ctx.lineTo(width, height / 2)
                    ctx.closePath()
                    ctx.fill()
                }
            }
        }
    }
    MouseArea {
        anchors.fill: playDisc
        onClicked: {
            root.togglePlay()
            root.interacted()
        }
    }

    // Settings GEAR in the TOP-RIGHT corner — opposite the bottom-right fullscreen button so the
    // two aren't fat-fingered. Collapses Quality + Speed (later Audio) into one uncluttered button;
    // tapping opens the playback menu.
    Rectangle {
        id: gearButton
        visible: (root.qualityEnabled && root.qualities.length > 0) || root.speedEnabled
                 || root.captionAvailable || root.audioAvailable
        anchors {
            right: parent.right; top: parent.top
            rightMargin: Theme.horizontalPageMargin; topMargin: Theme.paddingMedium
        }
        radius: Theme.paddingSmall
        color: root.playbackMenuOpen ? "#B0000000" : "#80000000"
        width: gearGlyph.width + 2 * Theme.paddingMedium
        height: gearGlyph.width + 2 * Theme.paddingMedium
        // A drawn gear (like the play/fullscreen glyphs) — no dependence on a theme icon.
        Canvas {
            id: gearGlyph
            anchors.centerIn: parent
            width: Theme.iconSizeSmall * 0.72
            height: width
            property bool lit: root.playbackMenuOpen
            onLitChanged: requestPaint()
            onWidthChanged: requestPaint()
            Component.onCompleted: requestPaint()
            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                ctx.strokeStyle = lit ? Theme.highlightColor : "white"
                ctx.lineWidth = Math.max(1.5, width * 0.11)
                ctx.lineJoin = "round"
                ctx.lineCap = "round"
                var cx = width / 2, cy = height / 2
                var R = width * 0.46, r = width * 0.31   // tooth-tip and valley radii
                var teeth = 8
                ctx.beginPath()
                for (var i = 0; i <= teeth * 2; i++) {   // alternate tip/valley → a toothed ring
                    var ang = Math.PI * i / teeth
                    var rad = (i % 2 === 0) ? R : r
                    var x = cx + rad * Math.cos(ang), yy = cy + rad * Math.sin(ang)
                    if (i === 0) ctx.moveTo(x, yy); else ctx.lineTo(x, yy)
                }
                ctx.closePath()
                ctx.stroke()
                ctx.beginPath()                          // hub
                ctx.arc(cx, cy, width * 0.14, 0, 2 * Math.PI)
                ctx.stroke()
            }
        }
        MouseArea {
            anchors.fill: parent
            anchors.topMargin: -Theme.paddingSmall
            anchors.bottomMargin: -Theme.paddingSmall
            anchors.rightMargin: -Theme.paddingSmall
            onClicked: {
                root.playbackMenuOpen = !root.playbackMenuOpen
                root.interacted()
            }
        }
    }

    // (No CC pill — captions live in the gear's Subtitles row now, so the top-left back-gesture
    // corner stays clear.)

    // --- thin scrubber pinned to the bottom, over a subtle gradient scrim ---
    Item {
        id: bar
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: fsButton.height + seekSlider.height + Theme.paddingSmall

        Rectangle {
            anchors.fill: parent
            gradient: Gradient {
                GradientStop { position: 0.0; color: "transparent" }
                GradientStop { position: 1.0; color: "#B0000000" }
            }
        }

        // Top row: time (left) · current chapter (centre) · fullscreen (right).
        Label {
            id: timeLabel
            anchors {
                left: parent.left; leftMargin: Theme.horizontalPageMargin
                verticalCenter: fsButton.verticalCenter
            }
            text: root.fmt(root.positionMs) + " / " + root.fmt(root.durationMs)
            font.pixelSize: Theme.fontSizeExtraSmall
            color: "white"
        }

        Label {
            id: chapterLabel
            visible: text.length > 0
            anchors {
                left: timeLabel.right; leftMargin: Theme.paddingMedium
                right: fsButton.left; rightMargin: Theme.paddingMedium
                verticalCenter: fsButton.verticalCenter
            }
            text: root.currentChapter
            truncationMode: TruncationMode.Fade
            horizontalAlignment: Text.AlignHCenter
            font.pixelSize: Math.round(Theme.fontSizeExtraSmall * 0.85)
            color: "#99FFFFFF"
        }

        // Fullscreen toggle: drawn corner brackets (outward = enter, inward = exit), so no
        // dependence on a theme icon that may not exist.
        Item {
            id: fsButton
            width: Theme.iconSizeSmall
            height: width
            anchors {
                right: parent.right; rightMargin: Theme.horizontalPageMargin
                top: parent.top; topMargin: Theme.paddingSmall / 2
            }
            Canvas {
                id: fsGlyph
                anchors.centerIn: parent
                width: parent.width * 0.62
                height: width
                property bool expanded: root.fullscreen
                onExpandedChanged: requestPaint()
                onWidthChanged: requestPaint()
                Component.onCompleted: requestPaint()
                onPaint: {
                    var ctx = getContext("2d")
                    ctx.reset()
                    ctx.strokeStyle = "white"
                    ctx.lineWidth = Math.max(1.5, width * 0.12)
                    ctx.lineCap = "round"
                    ctx.lineJoin = "round"
                    var S = width, a = S * 0.42
                    ctx.beginPath()
                    if (!expanded) {                 // enter: brackets hug the corners
                        ctx.moveTo(0, a);   ctx.lineTo(0, 0);     ctx.lineTo(a, 0)
                        ctx.moveTo(S - a, 0); ctx.lineTo(S, 0);   ctx.lineTo(S, a)
                        ctx.moveTo(0, S - a); ctx.lineTo(0, S);   ctx.lineTo(a, S)
                        ctx.moveTo(S - a, S); ctx.lineTo(S, S);   ctx.lineTo(S, S - a)
                    } else {                         // exit: brackets pulled inward
                        ctx.moveTo(0, a);   ctx.lineTo(a, a);       ctx.lineTo(a, 0)
                        ctx.moveTo(S, a);   ctx.lineTo(S - a, a);   ctx.lineTo(S - a, 0)
                        ctx.moveTo(0, S - a); ctx.lineTo(a, S - a); ctx.lineTo(a, S)
                        ctx.moveTo(S, S - a); ctx.lineTo(S - a, S - a); ctx.lineTo(S - a, S)
                    }
                    ctx.stroke()
                }
            }
            // Grow the tap target up and to the sides, but NOT down — the seek slider sits just
            // below, and a hit area that reached into it would steal taps meant for the scrubber.
            MouseArea {
                anchors.fill: parent
                anchors.topMargin: -Theme.paddingMedium
                anchors.leftMargin: -Theme.paddingMedium
                anchors.rightMargin: -Theme.paddingSmall
                onClicked: { root.toggleFullscreen(); root.interacted() }
            }
        }

        Slider {
            id: seekSlider
            anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
            leftMargin: Theme.horizontalPageMargin
            rightMargin: Theme.horizontalPageMargin
            minimumValue: 0
            maximumValue: Math.max(1, root.durationMs)
            handleVisible: true
            onReleased: {
                root.seekRequested(Math.round(seekSlider.value))
                root.interacted()
            }
        }

        // Chapter-boundary ticks on the scrubber track.
        Repeater {
            model: (root.chapters.length > 1 && root.durationMs > 0) ? root.chapters : 0
            Rectangle {
                width: 2
                height: Theme.paddingSmall
                radius: 1
                color: "#CCFFFFFF"
                x: Theme.horizontalPageMargin - width / 2
                   + (modelData.start * 1000 / root.durationMs)
                     * (bar.width - 2 * Theme.horizontalPageMargin)
                y: seekSlider.y + seekSlider.height / 2 - height / 2
            }
        }

        // Follow playback except while dragging (see the portrait-controls note in git
        // history): assigning on the tick, not a value-binding, so the released value
        // survives for onReleased.
        Connections {
            target: root
            onPositionMsChanged: if (!seekSlider.down) seekSlider.value = root.positionMs
        }
    }

    // Playback menu — the gear's two-tier sheet. Level 1: Quality / Speed rows (each with its
    // current value + a chevron). Level 2: the drill-in list for the chosen setting. A full-surface
    // scrim dims the video and swallows the outside tap that closes it.
    Item {
        id: playbackMenu
        anchors.fill: parent
        visible: root.playbackMenuOpen

        MouseArea {                                  // scrim: tap outside to dismiss
            anchors.fill: parent
            onClicked: { root.playbackMenuOpen = false; root.interacted() }
        }
        Rectangle { anchors.fill: parent; color: "#40000000" }

        Rectangle {
            id: playbackPanel
            // gearButton is an uncle (under root, not this menu), so anchor to the right edge and
            // drop the panel under the gear with a plain y binding (bindings may reference any id).
            anchors { right: parent.right; rightMargin: Theme.horizontalPageMargin }
            y: gearButton.y + gearButton.height + Theme.paddingSmall
            radius: Theme.paddingSmall
            color: "#E6000000"
            width: Math.min(root.width - 2 * Theme.horizontalPageMargin, root.width * 0.52)
            // Main list sizes to content; a drill-in list fills the room below the gear so a long
            // resolution/speed list scrolls inside the panel (in portrait the video is a strip).
            height: root.playbackSection === ""
                    ? Math.min(mainCol.height + 2 * Theme.paddingSmall,
                               root.height - y - Theme.paddingMedium)
                    : root.height - y - Theme.paddingMedium
            clip: true

            // --- Level 1: Subtitles / Quality / Speed rows (scrolls if all three don't fit) ---
            SilicaFlickable {
                visible: root.playbackSection === ""
                anchors.fill: parent
                anchors.topMargin: Theme.paddingSmall
                anchors.bottomMargin: Theme.paddingSmall
                contentHeight: mainCol.height
                Column {
                    id: mainCol
                    width: playbackPanel.width
                Rectangle {                              // Subtitles row
                    visible: root.captionAvailable
                    width: mainCol.width; height: Theme.itemSizeExtraSmall
                    color: cRow.pressed ? "#33FFFFFF" : "transparent"
                    Label {
                        anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                  verticalCenter: parent.verticalCenter }
                        text: "Subtitles"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                    }
                    Label {
                        anchors { right: cChev.left; rightMargin: Theme.paddingSmall
                                  verticalCenter: parent.verticalCenter }
                        // "Off", or the active base-language code (EN, PT…).
                        text: root.currentCaptionLang.length > 0
                              ? root.currentCaptionLang.split("-")[0].toUpperCase() : "Off"
                        font.pixelSize: Theme.fontSizeSmall
                        color: root.currentCaptionLang.length > 0 ? Theme.highlightColor : "#99FFFFFF"
                    }
                    Label { id: cChev
                        anchors { right: parent.right; rightMargin: Theme.horizontalPageMargin
                                  verticalCenter: parent.verticalCenter }
                        text: "›"; font.pixelSize: Theme.fontSizeLarge; color: "#99FFFFFF" }
                    MouseArea { id: cRow; anchors.fill: parent
                        onClicked: { root.playbackSection = "captions"; root.interacted() } }
                }
                Rectangle {                              // Audio row (dubbed videos only)
                    visible: root.audioAvailable
                    width: mainCol.width; height: Theme.itemSizeExtraSmall
                    color: aRow.pressed ? "#33FFFFFF" : "transparent"
                    Label {
                        anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                  verticalCenter: parent.verticalCenter }
                        text: "Audio"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                    }
                    Label {
                        anchors { right: aChev.left; rightMargin: Theme.paddingSmall
                                  verticalCenter: parent.verticalCenter
                                  left: parent.left; leftMargin: root.width * 0.28 }
                        text: root.currentAudioName()
                        truncationMode: TruncationMode.Fade; horizontalAlignment: Text.AlignRight
                        font.pixelSize: Theme.fontSizeSmall; color: Theme.highlightColor
                    }
                    Label { id: aChev
                        anchors { right: parent.right; rightMargin: Theme.horizontalPageMargin
                                  verticalCenter: parent.verticalCenter }
                        text: "›"; font.pixelSize: Theme.fontSizeLarge; color: "#99FFFFFF" }
                    MouseArea { id: aRow; anchors.fill: parent
                        onClicked: { root.playbackSection = "audio"; root.interacted() } }
                }
                Rectangle {                              // Quality row
                    visible: root.qualityEnabled && root.qualities.length > 0
                    width: mainCol.width; height: Theme.itemSizeExtraSmall
                    color: qRow.pressed ? "#33FFFFFF" : "transparent"
                    Label {
                        anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                  verticalCenter: parent.verticalCenter }
                        text: "Quality"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                    }
                    Label {
                        anchors { right: qChev.left; rightMargin: Theme.paddingSmall
                                  verticalCenter: parent.verticalCenter }
                        text: root.currentQuality.length > 0 ? root.currentQuality : "Auto"
                        font.pixelSize: Theme.fontSizeSmall; color: Theme.highlightColor
                    }
                    Label { id: qChev
                        anchors { right: parent.right; rightMargin: Theme.horizontalPageMargin
                                  verticalCenter: parent.verticalCenter }
                        text: "›"; font.pixelSize: Theme.fontSizeLarge; color: "#99FFFFFF" }
                    MouseArea { id: qRow; anchors.fill: parent
                        onClicked: { root.playbackSection = "quality"; root.interacted() } }
                }
                Rectangle {                              // Speed row
                    visible: root.speedEnabled
                    width: mainCol.width; height: Theme.itemSizeExtraSmall
                    color: sRow.pressed ? "#33FFFFFF" : "transparent"
                    Label {
                        anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                  verticalCenter: parent.verticalCenter }
                        text: "Speed"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                    }
                    Label {
                        anchors { right: sChev.left; rightMargin: Theme.paddingSmall
                                  verticalCenter: parent.verticalCenter }
                        text: root.fmtRate(root.playbackRate)
                        font.pixelSize: Theme.fontSizeSmall; color: Theme.highlightColor
                    }
                    Label { id: sChev
                        anchors { right: parent.right; rightMargin: Theme.horizontalPageMargin
                                  verticalCenter: parent.verticalCenter }
                        text: "›"; font.pixelSize: Theme.fontSizeLarge; color: "#99FFFFFF" }
                    MouseArea { id: sRow; anchors.fill: parent
                        onClicked: { root.playbackSection = "speed"; root.interacted() } }
                }
                }
                VerticalScrollDecorator {}
            }

            // --- Level 2: Quality list ---
            Item {
                visible: root.playbackSection === "quality"
                anchors.fill: parent
                Rectangle {
                    id: qBack
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    height: Theme.itemSizeExtraSmall
                    color: qBackArea.pressed ? "#33FFFFFF" : "transparent"
                    Label {
                        anchors { left: parent.left; leftMargin: Theme.paddingMedium
                                  verticalCenter: parent.verticalCenter }
                        text: "‹  Quality"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                    }
                    MouseArea { id: qBackArea; anchors.fill: parent
                        onClicked: { root.playbackSection = ""; root.interacted() } }
                }
                SilicaFlickable {
                    anchors { left: parent.left; right: parent.right
                              top: qBack.bottom; bottom: parent.bottom }
                    clip: true
                    contentHeight: qCol.height
                    Column {
                        id: qCol
                        width: parent.width
                        Repeater {
                            model: root.qualities
                            delegate: Rectangle {
                                width: qCol.width; height: Theme.itemSizeExtraSmall
                                color: qItemArea.pressed ? "#33FFFFFF" : "transparent"
                                Label {
                                    anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                              verticalCenter: parent.verticalCenter }
                                    text: modelData.label
                                    font.pixelSize: Theme.fontSizeSmall
                                    font.bold: modelData.label === root.currentQuality
                                    color: modelData.label === root.currentQuality
                                           ? Theme.highlightColor : "white"
                                }
                                MouseArea { id: qItemArea; anchors.fill: parent
                                    onClicked: { root.playbackMenuOpen = false
                                                 root.qualitySelected(modelData); root.interacted() } }
                            }
                        }
                    }
                    VerticalScrollDecorator {}
                }
            }

            // --- Level 2: Audio (dub) list ---
            Item {
                visible: root.playbackSection === "audio"
                anchors.fill: parent
                Rectangle {
                    id: aBack
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    height: Theme.itemSizeExtraSmall
                    color: aBackArea.pressed ? "#33FFFFFF" : "transparent"
                    Label {
                        anchors { left: parent.left; leftMargin: Theme.paddingMedium
                                  verticalCenter: parent.verticalCenter }
                        text: "‹  Audio"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                    }
                    MouseArea { id: aBackArea; anchors.fill: parent
                        onClicked: { root.playbackSection = ""; root.interacted() } }
                }
                SilicaFlickable {
                    anchors { left: parent.left; right: parent.right
                              top: aBack.bottom; bottom: parent.bottom }
                    clip: true
                    contentHeight: aCol.height
                    Column {
                        id: aCol
                        width: parent.width
                        Repeater {
                            model: root.audioTracks
                            delegate: Rectangle {
                                width: aCol.width; height: Theme.itemSizeExtraSmall
                                color: aItemArea.pressed ? "#33FFFFFF" : "transparent"
                                Label {
                                    anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                              right: parent.right; rightMargin: Theme.horizontalPageMargin
                                              verticalCenter: parent.verticalCenter }
                                    text: modelData.name + (modelData.is_original ? "  (original)" : "")
                                    truncationMode: TruncationMode.Fade
                                    font.pixelSize: Theme.fontSizeSmall
                                    font.bold: modelData.itag === root.currentAudioItag
                                    color: modelData.itag === root.currentAudioItag
                                           ? Theme.highlightColor : "white"
                                }
                                MouseArea { id: aItemArea; anchors.fill: parent
                                    onClicked: { root.playbackMenuOpen = false
                                                 root.audioSelected(modelData); root.interacted() } }
                            }
                        }
                    }
                    VerticalScrollDecorator {}
                }
            }

            // --- Level 2: Speed list ---
            Item {
                visible: root.playbackSection === "speed"
                anchors.fill: parent
                Rectangle {
                    id: sBack
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    height: Theme.itemSizeExtraSmall
                    color: sBackArea.pressed ? "#33FFFFFF" : "transparent"
                    Label {
                        anchors { left: parent.left; leftMargin: Theme.paddingMedium
                                  verticalCenter: parent.verticalCenter }
                        text: "‹  Speed"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                    }
                    MouseArea { id: sBackArea; anchors.fill: parent
                        onClicked: { root.playbackSection = ""; root.interacted() } }
                }
                SilicaFlickable {
                    anchors { left: parent.left; right: parent.right
                              top: sBack.bottom; bottom: parent.bottom }
                    clip: true
                    contentHeight: sCol.height
                    Column {
                        id: sCol
                        width: parent.width
                        Repeater {
                            model: root.speeds
                            delegate: Rectangle {
                                width: sCol.width; height: Theme.itemSizeExtraSmall
                                color: sItemArea.pressed ? "#33FFFFFF" : "transparent"
                                Label {
                                    anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                              verticalCenter: parent.verticalCenter }
                                    text: root.fmtRate(modelData) + (modelData === 1.0 ? "  (Normal)" : "")
                                    font.pixelSize: Theme.fontSizeSmall
                                    font.bold: modelData === root.playbackRate
                                    color: modelData === root.playbackRate
                                           ? Theme.highlightColor : "white"
                                }
                                MouseArea { id: sItemArea; anchors.fill: parent
                                    onClicked: { root.playbackMenuOpen = false
                                                 root.speedSelected(modelData); root.interacted() } }
                            }
                        }
                    }
                    VerticalScrollDecorator {}
                }
            }

            // --- Level 2: Subtitles — Off + real tracks + a Translate… drill-in ---
            Item {
                visible: root.playbackSection === "captions"
                anchors.fill: parent
                Rectangle {
                    id: capBack
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    height: Theme.itemSizeExtraSmall
                    color: capBackArea.pressed ? "#33FFFFFF" : "transparent"
                    Label {
                        anchors { left: parent.left; leftMargin: Theme.paddingMedium
                                  verticalCenter: parent.verticalCenter }
                        text: "‹  Subtitles"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                    }
                    MouseArea { id: capBackArea; anchors.fill: parent
                        onClicked: { root.playbackSection = ""; root.interacted() } }
                }
                SilicaFlickable {
                    anchors { left: parent.left; right: parent.right
                              top: capBack.bottom; bottom: parent.bottom }
                    clip: true
                    contentHeight: capCol.height
                    Column {
                        id: capCol
                        width: parent.width
                        Rectangle {                          // Off
                            width: capCol.width; height: Theme.itemSizeExtraSmall
                            color: capOffArea.pressed ? "#33FFFFFF" : "transparent"
                            Label {
                                anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                          verticalCenter: parent.verticalCenter }
                                text: "Off"; font.pixelSize: Theme.fontSizeSmall
                                font.bold: root.currentCaptionLang === ""
                                color: root.currentCaptionLang === "" ? Theme.highlightColor : "white"
                            }
                            MouseArea { id: capOffArea; anchors.fill: parent
                                onClicked: { root.playbackMenuOpen = false
                                             root.captionChosen(null); root.interacted() } }
                        }
                        Repeater {                           // manual + one ASR per language
                            model: root.captionTracks
                            delegate: Rectangle {
                                width: capCol.width; height: Theme.itemSizeExtraSmall
                                color: capTrackArea.pressed ? "#33FFFFFF" : "transparent"
                                Label {
                                    anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                              right: parent.right; rightMargin: Theme.paddingMedium
                                              verticalCenter: parent.verticalCenter }
                                    truncationMode: TruncationMode.Fade
                                    text: modelData.name + (modelData.kind === "asr"
                                          && modelData.name.toLowerCase().indexOf("auto") < 0
                                          ? "  ·  auto" : "")
                                    font.pixelSize: Theme.fontSizeSmall
                                    font.bold: modelData.lang === root.currentCaptionLang
                                    color: modelData.lang === root.currentCaptionLang
                                           ? Theme.highlightColor : "white"
                                }
                                MouseArea { id: capTrackArea; anchors.fill: parent
                                    onClicked: { root.playbackMenuOpen = false
                                                 root.captionChosen(modelData); root.interacted() } }
                            }
                        }
                        Rectangle {                          // Translate… drill-in
                            visible: root.captionTranslations.length > 0
                            width: capCol.width; height: Theme.itemSizeExtraSmall
                            color: capTrArea.pressed ? "#33FFFFFF" : "transparent"
                            Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top }
                                        height: 1; color: "#22FFFFFF" }
                            Label {
                                anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                          verticalCenter: parent.verticalCenter }
                                text: "Translate…"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                            }
                            Label {
                                anchors { right: parent.right; rightMargin: Theme.horizontalPageMargin
                                          verticalCenter: parent.verticalCenter }
                                text: "›"; font.pixelSize: Theme.fontSizeLarge; color: "#99FFFFFF" }
                            MouseArea { id: capTrArea; anchors.fill: parent
                                onClicked: { root.playbackSection = "translate"; root.interacted() } }
                        }
                    }
                    VerticalScrollDecorator {}
                }
            }

            // --- Level 3: Translate — a filtered search over the ~100 auto-translations ---
            Item {
                visible: root.playbackSection === "translate"
                anchors.fill: parent
                Rectangle {
                    id: trBack
                    anchors { left: parent.left; right: parent.right; top: parent.top }
                    height: Theme.itemSizeExtraSmall
                    color: trBackArea.pressed ? "#33FFFFFF" : "transparent"
                    Label {
                        anchors { left: parent.left; leftMargin: Theme.paddingMedium
                                  verticalCenter: parent.verticalCenter }
                        text: "‹  Translate to"; font.pixelSize: Theme.fontSizeSmall; color: "white"
                    }
                    MouseArea { id: trBackArea; anchors.fill: parent
                        onClicked: { root.playbackSection = "captions"; root.interacted() } }
                }
                SearchField {
                    id: captionSearch
                    anchors { left: parent.left; right: parent.right; top: trBack.bottom }
                    placeholderText: "Language"
                    onTextChanged: root.captionFilter = text
                }
                SilicaFlickable {
                    anchors { left: parent.left; right: parent.right
                              top: captionSearch.bottom; bottom: parent.bottom }
                    clip: true
                    contentHeight: trCol.height
                    Column {
                        id: trCol
                        width: parent.width
                        Repeater {
                            model: root.filteredTranslations()
                            delegate: Rectangle {
                                width: trCol.width; height: Theme.itemSizeExtraSmall
                                color: trItemArea.pressed ? "#33FFFFFF" : "transparent"
                                Label {
                                    anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                                              right: parent.right; rightMargin: Theme.paddingMedium
                                              verticalCenter: parent.verticalCenter }
                                    truncationMode: TruncationMode.Fade
                                    text: modelData.name
                                    font.pixelSize: Theme.fontSizeSmall
                                    font.bold: modelData.lang === root.currentCaptionLang
                                    color: modelData.lang === root.currentCaptionLang
                                           ? Theme.highlightColor : "white"
                                }
                                MouseArea { id: trItemArea; anchors.fill: parent
                                    onClicked: { root.playbackMenuOpen = false
                                                 root.captionChosen(modelData); root.interacted() } }
                            }
                        }
                    }
                    VerticalScrollDecorator {}
                }
            }
        }
    }


    function fmt(ms) {
        if (!ms || ms < 0)
            ms = 0
        var s = Math.floor(ms / 1000)
        var h = Math.floor(s / 3600)
        var m = Math.floor((s % 3600) / 60)
        var sec = s % 60
        function p(n) { return (n < 10 ? "0" : "") + n }
        return (h > 0 ? h + ":" + p(m) : "" + m) + ":" + p(sec)
    }

    // "1×", "1.25×", "0.5×" — whole rates drop the decimal.
    function fmtRate(r) {
        return (r === Math.floor(r) ? r.toFixed(0) : ("" + r)) + "×"
    }
}
