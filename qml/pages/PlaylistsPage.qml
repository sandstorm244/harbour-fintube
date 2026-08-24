import QtQuick 2.0
import Sailfish.Silica 1.0

// The playlist library: user-made local lists and saved YouTube playlists.
Page {
    id: page
    allowedOrientations: Orientation.All

    Component.onCompleted: app.backend.loadPlaylists()

    function fmtCount(n) { return (n === 1) ? "1 video" : ((n || 0) + " videos") }

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: app.backend.playlists

        PullDownMenu {
            MenuItem {
                text: "Add YouTube playlist"
                onClicked: pageStack.push(ytDialog)
            }
            MenuItem {
                text: "New playlist"
                onClicked: pageStack.push(newDialog)
            }
        }

        header: PageHeader { title: "Playlists" }

        delegate: ListItem {
            id: item
            width: listView.width
            contentHeight: Theme.itemSizeLarge

            menu: ContextMenu {
                MenuItem {
                    visible: modelData.kind === "youtube"
                    text: "Refresh from YouTube"
                    onClicked: app.backend.refreshPlaylist(modelData.id)
                }
                MenuItem {
                    text: "Delete"
                    onClicked: {
                        var pid = modelData.id, t = modelData.title
                        Remorse.popupAction(page, "Deleting " + t,
                            function() { app.backend.deletePlaylist(pid) })
                    }
                }
            }

            Rectangle {
                id: cover
                anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                          verticalCenter: parent.verticalCenter }
                height: Theme.itemSizeLarge - Theme.paddingMedium
                width: Math.round(height * 16 / 9)
                radius: Theme.paddingSmall / 2
                color: Theme.rgba(Theme.secondaryColor, 0.2)
                clip: true
                Image {
                    anchors.fill: parent
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    source: modelData.thumbnail || ""
                }
                // little YT badge for saved YouTube playlists
                Rectangle {
                    visible: modelData.kind === "youtube"
                    anchors { right: parent.right; bottom: parent.bottom; margins: 2 }
                    radius: 2
                    color: "#C8CC0000"
                    width: ytLabel.width + Theme.paddingSmall
                    height: ytLabel.height + 2
                    Label { id: ytLabel; anchors.centerIn: parent; text: "YT"
                            color: "white"; font.pixelSize: Theme.fontSizeTiny }
                }
            }
            Column {
                anchors { left: cover.right; leftMargin: Theme.paddingMedium
                          right: parent.right; rightMargin: Theme.horizontalPageMargin
                          verticalCenter: parent.verticalCenter }
                Label {
                    width: parent.width
                    text: modelData.title
                    truncationMode: TruncationMode.Fade
                    color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
                }
                Label {
                    width: parent.width
                    text: page.fmtCount(modelData.count)
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.secondaryColor
                }
            }

            onClicked: pageStack.push(Qt.resolvedUrl("PlaylistPage.qml"),
                { playlistId: modelData.id, playlistTitle: modelData.title })
        }

        ViewPlaceholder {
            enabled: app.backend.playlists.length === 0
            text: "No playlists"
            hintText: "Pull down to create one or add a YouTube playlist"
        }
        VerticalScrollDecorator { }
    }

    // --- New (local) playlist dialog ---
    Component {
        id: newDialog
        Dialog {
            id: dlg
            canAccept: nameField.text.trim().length > 0
            Column {
                width: parent.width
                DialogHeader { title: "New playlist" }
                TextField {
                    id: nameField
                    width: parent.width
                    label: "Name"
                    placeholderText: "Playlist name"
                    EnterKey.iconSource: "image://theme/icon-m-enter-accept"
                    EnterKey.onClicked: if (dlg.canAccept) dlg.accept()
                }
            }
            onAccepted: app.backend.createPlaylist(nameField.text.trim())
        }
    }

    // --- Add YouTube playlist dialog ---
    Component {
        id: ytDialog
        Dialog {
            id: yd
            canAccept: urlField.text.trim().length > 0
            Column {
                width: parent.width
                DialogHeader { title: "Add YouTube playlist" }
                TextField {
                    id: urlField
                    width: parent.width
                    label: "Playlist URL or ID"
                    placeholderText: "youtube.com/playlist?list=…"
                    EnterKey.iconSource: "image://theme/icon-m-enter-accept"
                    EnterKey.onClicked: if (yd.canAccept) yd.accept()
                }
                Label {
                    x: Theme.horizontalPageMargin
                    width: parent.width - 2 * Theme.horizontalPageMargin
                    wrapMode: Text.Wrap
                    text: "Fetches the playlist's videos and saves it to your library."
                    color: Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                }
            }
            onAccepted: app.backend.saveYoutubePlaylist(urlField.text.trim())
        }
    }
}
