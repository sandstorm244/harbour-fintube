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
    property bool qualityMenuOpen: false // parent reads this to hold the controls open

    signal seekRequested(int ms)
    signal togglePlay()
    signal cycleSpeed()
    signal qualitySelected(var q)
    signal toggleFullscreen()
    signal interacted()

    // A Canvas drops its buffer when the app is backgrounded, so the drawn glyphs come back
    // blank. Repaint them when the app returns to the foreground and when the controls are
    // re-shown (the parent toggles `enabled`).
    onEnabledChanged: if (enabled) { glyph.requestPaint(); fsGlyph.requestPaint() }
    Connections {
        target: Qt.application
        onActiveChanged: if (Qt.application.active) {
            glyph.requestPaint()
            fsGlyph.requestPaint()
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

    // Playback-speed pill in the TOP-RIGHT corner — deliberately the opposite corner from
    // the bottom-right fullscreen button so the two aren't fat-fingered.
    Rectangle {
        id: speedButton
        visible: root.speedEnabled
        anchors {
            right: parent.right; top: parent.top
            rightMargin: Theme.horizontalPageMargin; topMargin: Theme.paddingMedium
        }
        radius: Theme.paddingSmall
        color: "#80000000"
        width: speedLabel.width + 2 * Theme.paddingMedium
        height: speedLabel.height + 2 * Theme.paddingMedium
        Label {
            id: speedLabel
            anchors.centerIn: parent
            text: (root.playbackRate === Math.floor(root.playbackRate)
                   ? root.playbackRate.toFixed(0) : ("" + root.playbackRate)) + "×"
            font.pixelSize: Theme.fontSizeSmall
            font.bold: root.playbackRate !== 1.0
            color: root.playbackRate !== 1.0 ? Theme.highlightColor : "white"
        }
        // Forgiving hit area: expand outward (up/down + the outer, right edge) but NOT toward
        // the quality pill, so the two adjacent targets never contest the gap between them.
        MouseArea {
            anchors.fill: parent
            anchors.topMargin: -Theme.paddingSmall
            anchors.bottomMargin: -Theme.paddingSmall
            anchors.rightMargin: -Theme.paddingSmall
            onClicked: { root.cycleSpeed(); root.interacted() }
        }
    }

    // Quality pill — sits just LEFT of the speed pill. Unlike speed (a quick cycle), tapping
    // this opens a small overlay menu of resolutions, so picking 1080p is one deliberate tap
    // rather than cycling through every step.
    Rectangle {
        id: qualityButton
        visible: root.qualityEnabled && root.qualities.length > 0
        anchors {
            right: speedButton.left; rightMargin: Theme.paddingSmall
            top: speedButton.top
        }
        radius: Theme.paddingSmall
        color: root.qualityMenuOpen ? "#B0000000" : "#80000000"
        width: qualityLabel.width + 2 * Theme.paddingMedium
        height: qualityLabel.height + 2 * Theme.paddingMedium
        Label {
            id: qualityLabel
            anchors.centerIn: parent
            text: root.currentQuality.length > 0 ? root.currentQuality : "Auto"
            font.pixelSize: Theme.fontSizeSmall
            color: root.qualityMenuOpen ? Theme.highlightColor : "white"
        }
        // Forgiving hit area: expand outward (up/down + the outer, left edge) but NOT toward
        // the speed pill, so the two adjacent targets never contest the gap between them.
        MouseArea {
            anchors.fill: parent
            anchors.topMargin: -Theme.paddingSmall
            anchors.bottomMargin: -Theme.paddingSmall
            anchors.leftMargin: -Theme.paddingSmall
            onClicked: {
                root.qualityMenuOpen = !root.qualityMenuOpen
                root.interacted()
            }
        }
    }

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

    // Quality menu — a compact overlay list under the top-right pills, present only while
    // open. Declared LAST so it draws over the scrubber and buttons; a full-surface scrim
    // dims the video and swallows the outside tap that closes it (so that same tap does not
    // also toggle the controls beneath).
    Item {
        id: qualityMenu
        anchors.fill: parent
        visible: root.qualityMenuOpen

        MouseArea {                                  // scrim: tap outside to dismiss
            anchors.fill: parent
            onClicked: { root.qualityMenuOpen = false; root.interacted() }
        }
        Rectangle { anchors.fill: parent; color: "#40000000" }   // subtle dim behind the list

        Rectangle {
            id: qualityPanel
            // speedButton lives under root, not this panel's parent (qualityMenu), so it can't
            // be an anchor *target* here — QML only anchors to a parent/sibling. qualityMenu
            // fills root, so speedButton's geometry is in the same coordinate space: drop the
            // panel just below the pills with a plain y binding (which may reference any item).
            anchors { right: parent.right; rightMargin: Theme.horizontalPageMargin }
            y: speedButton.y + speedButton.height + Theme.paddingSmall
            radius: Theme.paddingSmall
            color: "#E6000000"
            width: qualityColumn.width + 2 * Theme.paddingMedium
            // Cap to the room below the pills: in portrait the video is only a strip, so a
            // long resolution list scrolls inside the panel instead of spilling over the
            // description beneath it. In fullscreen there's ample height and it never scrolls.
            height: Math.min(qualityColumn.height + 2 * Theme.paddingSmall,
                             root.height - y - Theme.paddingMedium)
            clip: true
            SilicaFlickable {
                anchors.fill: parent
                anchors.topMargin: Theme.paddingSmall
                anchors.bottomMargin: Theme.paddingSmall
                contentHeight: qualityColumn.height
                Column {
                    id: qualityColumn
                    x: Theme.paddingMedium        // inset both sides (panel adds 2×paddingMedium)
                    Repeater {
                        model: root.qualities
                        delegate: Rectangle {
                            // min width keeps the short labels (144p) as wide as the long ones,
                            // so the rows read as a tidy column rather than a ragged one.
                            width: Math.max(rowLabel.width + 2 * Theme.paddingMedium,
                                            Theme.itemSizeExtraSmall * 1.6)
                            height: Theme.itemSizeExtraSmall
                            color: rowArea.pressed ? "#33FFFFFF" : "transparent"
                            Label {
                                id: rowLabel
                                anchors {
                                    left: parent.left; leftMargin: Theme.paddingMedium
                                    verticalCenter: parent.verticalCenter
                                }
                                text: modelData.label
                                font.pixelSize: Theme.fontSizeSmall
                                font.bold: modelData.label === root.currentQuality
                                color: modelData.label === root.currentQuality
                                       ? Theme.highlightColor : "white"
                            }
                            MouseArea {
                                id: rowArea
                                anchors.fill: parent
                                onClicked: {
                                    root.qualityMenuOpen = false
                                    root.qualitySelected(modelData)
                                    root.interacted()
                                }
                            }
                        }
                    }
                }
                VerticalScrollDecorator {}
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
}
