import QtQuick 2.0
import Sailfish.Silica 1.0

// App cover. When something is playing it shows the title + channel and offers a
// play/pause cover action, so playback can be controlled from the home screen without
// reopening the app. Values are bound to app.nowPlaying by the inline cover Component.
CoverBackground {
    id: cover

    property bool active: false
    property string title: ""
    property string channel: ""
    property bool playing: false
    signal toggle()

    Column {
        anchors {
            left: parent.left; right: parent.right
            verticalCenter: parent.verticalCenter
            leftMargin: Theme.paddingMedium; rightMargin: Theme.paddingMedium
        }
        spacing: Theme.paddingSmall

        Label {
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: cover.active ? "Now playing" : "FinTube"
            font.pixelSize: cover.active ? Theme.fontSizeExtraSmall : Theme.fontSizeLarge
            color: cover.active ? Theme.secondaryColor : Theme.highlightColor
        }
        Label {
            visible: cover.active && text.length > 0
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.Wrap
            maximumLineCount: 3
            elide: Text.ElideRight
            text: cover.title
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.primaryColor
        }
        Label {
            visible: cover.active && text.length > 0
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            truncationMode: TruncationMode.Fade
            text: cover.channel
            font.pixelSize: Theme.fontSizeExtraSmall
            color: Theme.secondaryColor
        }
    }

    CoverActionList {
        enabled: cover.active
        CoverAction {
            iconSource: cover.playing ? "image://theme/icon-cover-pause"
                                      : "image://theme/icon-cover-play"
            onTriggered: cover.toggle()
        }
    }
}
