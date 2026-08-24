import QtQuick 2.0
import Sailfish.Silica 1.0

// Drop inside a video thumbnail (anchors.fill: parent). Reads Backend.watchMap for `videoId`
// and draws the YouTube-style watch-progress bar along the bottom edge + a WATCHED badge in
// the corner. Purely decorative (no MouseArea), so taps pass through to the delegate below.
Item {
    id: overlay
    property string videoId: ""
    // Look the entry up in the shared map; rebinds whenever watchMap is reassigned (after a save).
    property var entry: (videoId && app.backend.watchMap) ? app.backend.watchMap[videoId] : undefined
    property real fraction: entry ? Math.max(0, Math.min(1, entry.f || 0)) : 0
    property bool watched: !!(entry && entry.w)

    // Progress track + played portion, hugging the bottom edge (like YouTube's).
    Rectangle {
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: Math.max(Theme.paddingSmall / 2, Math.round(overlay.height * 0.05))
        color: "#66000000"                       // faint track behind the played bar
        visible: overlay.fraction > 0.005
        Rectangle {
            anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
            width: Math.round(parent.width * overlay.fraction)
            color: "#ff0000"                      // classic watch-progress red
        }
    }

    // WATCHED badge, top-left corner (matches the duration badge's dark-pill styling).
    Rectangle {
        visible: overlay.watched
        anchors { top: parent.top; left: parent.left; margins: Theme.paddingSmall / 2 }
        radius: 3
        color: "#C8000000"
        width: watchedLabel.width + Theme.paddingSmall
        height: watchedLabel.height + Theme.paddingSmall / 2
        Label {
            id: watchedLabel
            anchors.centerIn: parent
            text: "WATCHED"
            font.pixelSize: Theme.fontSizeExtraSmall
            font.bold: true
            color: "white"
        }
    }
}
