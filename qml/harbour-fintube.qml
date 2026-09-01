import QtQuick 2.0
import Sailfish.Silica 1.0
import Nemo.DBus 2.0
import Sailfish.Pickers 1.0
import FinTube 1.0
import "pages"
import "cover"

ApplicationWindow {
    id: app

    // Shared Python backend, reachable from any page as `app.backend`.
    property alias backend: backend
    // App-level "what's playing", reachable as `app.nowPlaying`.
    property alias nowPlaying: nowPlaying
    // The single, persistent GStreamer player. It lives here (not inside VideoPage) so audio keeps
    // playing when you leave the video page to browse elsewhere: VideoPage reparents it into its
    // video box while open and hands it back to playerHolder on close. Reachable as `app.gplayer`.
    property alias gplayer: gplayer
    // Audio-effects target for the Equalizer / volume-boost pages — always the singleton player
    // (its C++ setters are cheap no-ops until a pipeline is built).
    readonly property alias activePlayer: gplayer
    // True while gplayer plays in the background with no VideoPage attached — the app-level
    // controller (bgConn) then drives cover / lockscreen toggle+stop in the page's place.
    property bool bgAudioActive: false
    // Last resolve()d info, kept so reopening the still-playing video repopulates its page
    // (description, comments, quality/caption menus) instantly — no re-resolve, no restart.
    property var lastResolvedInfo: null
    property string lastResolvedId: ""
    // The most recently left video, for quick-resume from the home page: {id, title, channel}.
    property var lastVideo: null
    // Autoplay queue for playlist playback: [{id,title}, …] + the index currently playing.
    property var playQueue: []
    property int playQueueIndex: -1

    // Push the persisted equalizer + volume-boost settings onto the live player. The C++ setters
    // are cheap no-ops when the pipeline isn't built yet — the values persist and apply at its
    // next build — so this is safe to call any time.
    function applyAudioFx() {
        if (!activePlayer)
            return
        activePlayer.setEqEnabled(backend.eqEnabled)
        var b = backend.eqBands || []
        for (var i = 0; i < 10; i++)
            activePlayer.setEqBand(i, b[i] || 0)
        activePlayer.setBoost(backend.boostGain)
    }

    // Hand the singleton player back to its off-screen home (VideoPage calls this on close, since
    // playerHolder is a private id of this component). Does NOT stop playback — the caller decides
    // whether audio keeps going in the background or is stopped.
    function parkPlayer() {
        gplayer.anchors.fill = undefined
        gplayer.visible = false
        gplayer.parent = playerHolder
        gplayer.anchors.fill = playerHolder
    }

    // Re-apply live whenever a setting changes (e.g. from the Equalizer page or the boost slider).
    Connections {
        target: backend
        onEqEnabledChanged: app.applyAudioFx()
        onEqBandsChanged: app.applyAudioFx()
        onBoostGainChanged: app.applyAudioFx()
    }

    // Dispatch a More-page ACTION row (see MorePage.qml) — entries carry a string `action` key
    // (functions can't survive the var-array model), which MorePage routes here.
    function moreAction(key) {
        if (key === "import-newpipe") importNewpipe()
        else if (key === "youtube-import") importYoutubeAccount()
    }

    // --- Live YouTube account import: subscriptions + playlists (Home → More → "Import from YouTube") ---
    // Needs an imported browser login (More → Providers → YouTube account). Two network calls;
    // a toast reports the combined result. Guarded so a signed-out tap explains itself.
    function importYoutubeAccount() {
        if (!app.backend.youtubeLoggedIn) {
            app.showToast("Sign in first: More → Providers → YouTube account")
            return
        }
        var win = app
        win.showToast("Importing from YouTube…")
        win.backend.importYoutubeAccount(function(res) {
            if (!res || !res.ok) {
                win.showToast((res && res.error) ? res.error : "Import failed")
                return
            }
            win.showToast(res.summary || "Imported from YouTube")
        })
    }

    // --- NewPipe / PipePipe subscription import (Home → More → "Import subscriptions") ---
    // Opens a file picker; the chosen .zip/newpipe.db is parsed by the engine and its YouTube
    // channel subscriptions merged in. A brief toast reports the result.
    function importNewpipe() {
        pageStack.push(newpipePicker)
    }
    Component {
        id: newpipePicker
        FilePickerPage {
            title: "Choose a NewPipe / PipePipe backup"
            nameFilters: [ "*.zip", "*.db" ]
            onSelectedContentPropertiesChanged: {
                // Capture the ApplicationWindow as a plain JS reference: the pyotherside callback
                // below is invoked from Backend's QML context (QJSValue.call), where the `app` ID
                // won't resolve — but a captured object reference the closure holds still works.
                var win = app
                win.showToast("Importing…")
                win.backend.importNewpipe(selectedContentProperties.filePath, function(res) {
                    if (!res || !res.ok) {
                        win.showToast((res && res.error) ? res.error : "Import failed")
                        return
                    }
                    var msg = res.summary || ("Imported " + res.added + " channel"
                                              + (res.added === 1 ? "" : "s"))
                    win.showToast(msg)
                })
            }
        }
    }

    // Transient status toast (used by the import; reusable for any brief app-wide feedback).
    property string toastText: ""
    function showToast(msg) { app.toastText = msg || ""; toastTimer.restart() }
    Timer { id: toastTimer; interval: 3000; onTriggered: app.toastText = "" }

    initialPage: Component { HomePage { } }

    // Inline so the cover shares this document's scope and can bind to `nowPlaying`
    // (a separate CoverPage.qml file can't reach an id declared here).
    cover: Component {
        CoverPage {
            active: nowPlaying.active
            title: nowPlaying.title
            channel: nowPlaying.channel
            playing: nowPlaying.playing
            onToggle: nowPlaying.toggleRequested()
        }
    }

    allowedOrientations: defaultAllowedOrientations

    // The active video page keeps this in sync; the cover + MPRIS read it. stopRequested lets a
    // newly-started source tell the previous one to stop, so only one thing ever plays at a time.
    QtObject {
        id: nowPlaying
        property bool active: false
        property string videoId: ""      // the gplayer video currently loaded ("" = none/other source)
        property string title: ""
        property string channel: ""
        property bool playing: false
        signal toggleRequested()
        signal stopRequested()
    }

    Backend { id: backend }

    // Off-screen home for the singleton player when no VideoPage is showing it (audio keeps
    // playing here; the video branch is frozen). VideoPage reparents gplayer into its video box.
    Item { id: playerHolder; visible: false; width: 0; height: 0 }

    VideoPlayer {
        id: gplayer
        parent: playerHolder
        anchors.fill: parent
        visible: false
        // Set before playback so the player picks its FBO render target + pipeline branch.
        hwDecode: backend.hwDecode
    }

    // Background controller: while gplayer plays with no VideoPage attached (bgAudioActive), the
    // page's own nowPlaying handler is gone, so the app drives cover / lockscreen toggle + stop.
    Connections {
        target: app.bgAudioActive ? nowPlaying : null
        onToggleRequested: gplayer.playing ? gplayer.pause() : gplayer.play()
        onStopRequested: {
            gplayer.stop()
            nowPlaying.active = false
            nowPlaying.videoId = ""
            app.bgAudioActive = false
        }
    }
    // Keep the cover / lockscreen play-state honest while backgrounded (a page in front does its
    // own sync). If the backgrounded track ends, reflect the stop but leave the cover claimed.
    Connections {
        target: gplayer
        onPlayingChanged: if (app.bgAudioActive) nowPlaying.playing = gplayer.playing
        onEnded: if (app.bgAudioActive) nowPlaying.playing = false
        // No page is attached to recover a background stall (a 403 etc.), so just reflect that it
        // stopped — reopening the video re-resolves and resumes from the saved position.
        onErrorOccurred: if (app.bgAudioActive) nowPlaying.playing = false
    }

    // Optional MPRIS (lockscreen) controls, isolated so a missing plugin degrades quietly.
    Loader {
        source: Qt.resolvedUrl("MprisControls.qml")
        onLoaded: item.np = nowPlaying
        onStatusChanged: if (status === Loader.Error)
            console.log("FinTube: MPRIS unavailable (org.nemomobile.mpris not installed)")
    }

    // A link that arrived before the Python backend finished importing (cold D-Bus activation);
    // held here and flushed once backend.pyReady flips.
    property string pendingUrl: ""

    Connections {
        target: backend
        onPyReadyChanged: if (backend.pyReady && app.pendingUrl.length > 0) {
            var u = app.pendingUrl
            app.pendingUrl = ""
            app.routeUrl(u)
        }
    }

    // Stop the PO-token sidecar cleanly when the app quits. A kernel PDEATHSIG on the child
    // is the real guarantee (it fires even on a hard kill); this is the graceful path.
    Connections {
        target: Qt.application
        onAboutToQuit: backend.stopPotServer()
    }

    // Open incoming youtube.com / youtu.be links (from the browser or another app). The URL
    // dispatcher calls this D-Bus method (see the .desktop X-Maemo-Service); Python classifies
    // the link and we route to the right page. Videos + channels now; playlists land in Batch C.
    function routeUrl(url) {
        if (!url)
            return
        if (!backend.pyReady) {   // module still importing (cold launch) — buffer, flush on ready
            app.pendingUrl = url
            return
        }
        backend.parseUrl(url, function(res) {
            if (!res || !res.kind)
                return
            if (res.kind === "video")
                pageStack.push(Qt.resolvedUrl("pages/VideoPage.qml"),
                    { videoId: res.id || res.url, title: "" })
            else if (res.kind === "channel")
                pageStack.push(Qt.resolvedUrl("pages/ChannelPage.qml"),
                    { channelRef: res.url, channelName: "", channelId: res.id || "", channelThumb: "" })
            else if (res.kind === "playlist") {
                // Save the playlist into the library (async), then show the library — it fills in
                // once the fetch completes since the page binds to backend.playlists.
                backend.saveYoutubePlaylist(res.id || res.url)
                pageStack.push(Qt.resolvedUrl("pages/PlaylistsPage.qml"))
            }
        })
    }

    DBusAdaptor {
        id: urlHandler
        service: "harbour.fintube"
        path: "/"
        iface: "harbour.fintube"
        xml: '<interface name="harbour.fintube">\n' +
             '  <method name="openUrl"><arg type="s" name="url" direction="in"/></method>\n' +
             '</interface>'
        function openUrl(url) {
            app.routeUrl(url)   // route first so a link still opens even if activate() is a no-op
            app.activate()
        }
    }

    // App-wide quick-resume bar — floats over every page (feed, channel, search) so you can
    // jump back into the last video at its saved spot. Hidden while watching (opening a
    // video clears lastVideo); dismissable via the ✕.
    Item {
        id: resumeBar
        z: 1000
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: Theme.itemSizeMedium
        // Hidden on pages that opt out (More + its utility pages set hideDock) so it can't cover
        // their fixed bottom controls; still shows over scrollable list pages, which scroll clear.
        visible: lastVideo && lastVideo.id && lastVideo.id.length > 0
                 && !(pageStack.currentPage && pageStack.currentPage.hideDock === true)

        Rectangle {
            anchors.fill: parent
            color: "#E6000000"        // dark neutral panel — accent stays a detail, below
        }
        Rectangle {                   // thin accent line along the top edge
            anchors { left: parent.left; right: parent.right; top: parent.top }
            height: Math.max(2, Math.round(2 * Theme.pixelRatio))
            color: Theme.highlightColor
        }
        MouseArea {
            anchors.fill: parent
            onClicked: pageStack.push(Qt.resolvedUrl("pages/VideoPage.qml"),
                { videoId: lastVideo.id, title: lastVideo.title || "" })
        }
        Image {
            id: rbIcon
            anchors {
                left: parent.left; leftMargin: Theme.horizontalPageMargin
                verticalCenter: parent.verticalCenter
            }
            source: "image://theme/icon-m-play"
            width: Theme.iconSizeMedium; height: width
        }
        Column {
            anchors {
                left: rbIcon.right; leftMargin: Theme.paddingMedium
                right: rbClose.left; rightMargin: Theme.paddingMedium
                verticalCenter: parent.verticalCenter
            }
            Label {
                width: parent.width
                text: "Continue watching"
                font.pixelSize: Theme.fontSizeExtraSmall
                color: Theme.secondaryColor
            }
            Label {
                width: parent.width
                text: lastVideo ? (lastVideo.title || "") : ""
                truncationMode: TruncationMode.Fade
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.primaryColor
            }
        }
        IconButton {
            id: rbClose
            anchors {
                right: parent.right; rightMargin: Theme.horizontalPageMargin
                verticalCenter: parent.verticalCenter
            }
            icon.source: "image://theme/icon-m-clear"
            onClicked: app.lastVideo = null
        }
    }

    // Transient toast banner — floats at the bottom, above the resume bar when it's showing.
    Rectangle {
        z: 2000
        visible: app.toastText.length > 0
        anchors {
            horizontalCenter: parent.horizontalCenter
            bottom: parent.bottom
            bottomMargin: (resumeBar.visible ? resumeBar.height : 0) + Theme.paddingLarge
        }
        width: Math.min(app.width - 2 * Theme.horizontalPageMargin,
                        toastLabel.implicitWidth + 2 * Theme.paddingLarge)
        height: toastLabel.paintedHeight + 2 * Theme.paddingMedium
        radius: Theme.paddingMedium
        color: "#E6000000"
        Label {
            id: toastLabel
            anchors.centerIn: parent
            width: Math.min(implicitWidth, app.width - 4 * Theme.horizontalPageMargin)
            text: app.toastText
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            color: "white"
            font.pixelSize: Theme.fontSizeSmall
        }
    }
}
