import QtQuick 2.0
import Sailfish.Silica 1.0
import QtMultimedia 5.6

// Plays a downloaded local file (muxed mp4 or m4a) with QtMultimedia — no yt-dlp, no proxy,
// since the file is a plain container. Reuses TransportControls and the now-playing/cover
// plumbing so downloaded audio still works on the cover + lockscreen.
Page {
    id: page
    allowedOrientations: Orientation.All

    property string title: ""
    property string path: ""        // local file path
    property string kind: "video"   // "audio" | "video"
    property string errorText: ""

    property bool landscape: page.orientation === Orientation.Landscape
                             || page.orientation === Orientation.LandscapeInverted
    property bool controlsShown: true

    // Portrait-only drop below the physical camera cutout (OS geometry), matching the online player.
    // Guarded — Screen.hasCutouts / Screen.topCutout only exist on newer Silica.
    property real notchOffset: (typeof Screen !== "undefined" && Screen.hasCutouts && Screen.topCutout)
                               ? (Screen.topCutout.y + Screen.topCutout.height)
                               : 0

    property int positionMs: mediaPlayer.position
    property int durationMs: mediaPlayer.duration
    property bool isPlaying: mediaPlayer.playbackState === MediaPlayer.PlayingState

    function togglePlay() {
        if (mediaPlayer.playbackState === MediaPlayer.PlayingState)
            mediaPlayer.pause()
        else
            mediaPlayer.play()
    }
    function toggleFullscreen() {
        page.allowedOrientations = page.landscape ? Orientation.Portrait : Orientation.Landscape
    }

    MediaPlayer {
        id: mediaPlayer
        source: page.path ? ("file://" + page.path) : ""
        autoPlay: true
        onError: page.errorText = errorString
    }

    // Now-playing (cover + MPRIS), same as the online player.
    function claimNowPlaying() {
        app.nowPlaying.stopRequested()
        app.nowPlaying.title = page.title
        app.nowPlaying.channel = "Downloaded"
        app.nowPlaying.playing = page.isPlaying
        app.nowPlaying.active = true
        nowPlayingConn.target = app.nowPlaying
    }
    Connections {
        id: nowPlayingConn
        target: null
        onToggleRequested: page.togglePlay()
        onStopRequested: { mediaPlayer.stop(); nowPlayingConn.target = null }
    }
    onIsPlayingChanged: {
        if (!page.isPlaying)
            page.controlsShown = true
        if (nowPlayingConn.target)
            app.nowPlaying.playing = page.isPlaying
    }

    Component.onCompleted: {
        app.lastVideo = null
        page.claimNowPlaying()
    }
    Component.onDestruction: {
        if (nowPlayingConn.target)
            app.nowPlaying.active = false
        mediaPlayer.stop()
    }

    // Notch spacer (portrait only): a black strip so the camera cutout sits over black, with the
    // video dropped below it rather than clipped.
    Rectangle {
        visible: !page.landscape && page.notchOffset > 0
        color: "black"
        anchors { top: parent.top; left: parent.left; right: parent.right }
        height: page.notchOffset
    }

    Rectangle {
        id: videoBox
        color: "black"
        anchors { top: parent.top; left: parent.left; right: parent.right
                  topMargin: page.landscape ? 0 : page.notchOffset }
        height: page.landscape ? page.height : Math.round(width * 9 / 16)

        VideoOutput {
            anchors.fill: parent
            source: mediaPlayer
            fillMode: VideoOutput.PreserveAspectFit
            visible: page.kind === "video"
        }
        Label {   // audio: show the title in the black box
            anchors.centerIn: parent
            visible: page.kind === "audio" && page.errorText.length === 0
            width: parent.width - 2 * Theme.paddingLarge
            text: page.title
            color: "white"
            wrapMode: Text.Wrap
            maximumLineCount: 3
            elide: Text.ElideRight
            horizontalAlignment: Text.AlignHCenter
            font.pixelSize: Theme.fontSizeMedium
        }
        Label {
            anchors.centerIn: parent
            visible: page.errorText.length > 0
            width: parent.width - 2 * Theme.paddingLarge
            text: page.errorText
            color: Theme.errorColor
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
        }

        MouseArea {
            anchors.fill: parent
            onClicked: page.controlsShown = !page.controlsShown
        }
        TransportControls {
            anchors.fill: parent
            enabled: page.controlsShown
            opacity: page.controlsShown ? 1 : 0
            Behavior on opacity { FadeAnimation {} }
            positionMs: page.positionMs
            durationMs: page.durationMs
            isPlaying: page.isPlaying
            speedEnabled: false
            fullscreen: page.landscape
            onSeekRequested: mediaPlayer.seek(ms)
            onTogglePlay: page.togglePlay()
            onToggleFullscreen: page.toggleFullscreen()
            onInteracted: page.controlsShown = true
        }
    }
    Timer {
        interval: 3500
        running: page.controlsShown && page.isPlaying
        onTriggered: page.controlsShown = false
    }

    SilicaFlickable {
        visible: !page.landscape
        anchors {
            top: videoBox.bottom; bottom: parent.bottom
            left: parent.left; right: parent.right
        }
        contentHeight: col.height
        Column {
            id: col
            width: parent.width
            PageHeader { title: page.title }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                text: page.kind === "audio" ? "Downloaded audio" : "Downloaded video"
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.secondaryColor
            }
        }
    }
}
