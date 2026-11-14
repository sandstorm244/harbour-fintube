import QtQuick 2.0
import "../logic.js" as Logic

// Holds the display awake while a video is visibly playing, and hands the screen back to the
// system's own timeout the moment it isn't (Logic.keepAwake). "playing" alone is not trusted: a
// player can keep reporting it after an error or a stream that died without an end, so the
// position must also have moved within Logic.AWAKE_STALL_MS. Drop one into a player page.
Item {
    id: root
    property bool playing: false
    property bool onScreen: true
    property int positionMs: 0

    property double movedAt: 0      // Date.now() of the last start or position change
    property bool stalled: false
    readonly property bool hold: Logic.keepAwake(root.playing, root.onScreen,
                                                 Qt.application.active, root.stalled)

    // A fresh start gets a full window to preroll; every position change re-arms it.
    onPlayingChanged: { root.movedAt = Date.now(); root.stalled = false }
    onPositionMsChanged: { root.movedAt = Date.now(); root.stalled = false }
    onHoldChanged: console.log("FinTube: display hold " + (root.hold ? "on" : "off"))

    Timer {
        interval: 5000
        repeat: true
        running: root.playing && !root.stalled
        onTriggered: root.stalled = Logic.awakeStalled(Date.now() - root.movedAt)
    }

    // Loaded once and kept for the page's life; the hold is switched through preventBlanking,
    // which tells the system at once. The old code unloaded it instead and left the release to the
    // object's deferred deletion: on device the hold then kept renewing after the video ended.
    // Isolated in a Loader so a device without the Nemo.KeepAlive plugin degrades quietly.
    Loader {
        id: blanking
        source: Qt.resolvedUrl("KeepDisplayOn.qml")
        onStatusChanged: if (status === Loader.Error)
            console.log("FinTube: Nemo.KeepAlive unavailable — the display can't be held awake")
    }
    Binding {
        target: blanking.item
        property: "preventBlanking"
        value: root.hold
        when: blanking.status === Loader.Ready
    }
}
