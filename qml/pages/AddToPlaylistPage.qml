import QtQuick 2.0
import Sailfish.Silica 1.0

// Picker shown by "Add to playlist" (long-press). Lists local playlists to add the video to,
// with a shortcut to make a new one. (Saved YouTube playlists are mirrors of YouTube, so we
// don't add to those.)
Page {
    id: page
    allowedOrientations: Orientation.All

    property var video: null    // {id, title, uploader, duration, thumbnail}

    // Only local playlists can be added to; re-derives when the library changes.
    property var localPlaylists: {
        var out = []
        var pl = app.backend.playlists
        for (var i = 0; i < pl.length; i++)
            if (pl[i].kind === "local") out.push(pl[i])
        return out
    }

    Component.onCompleted: app.backend.loadPlaylists()

    function addTo(plId) {
        app.backend.addToPlaylist(plId, page.video || {})
        pageStack.pop()
    }

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: page.localPlaylists

        header: Column {
            width: listView.width
            PageHeader { title: "Add to playlist" }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                visible: text.length > 0
                text: page.video ? (page.video.title || "") : ""
                truncationMode: TruncationMode.Fade
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeSmall
            }
            Item { width: 1; height: Theme.paddingMedium }
            BackgroundItem {
                id: newItem
                width: parent.width
                height: Theme.itemSizeMedium
                Image {
                    id: plusIcon
                    anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                              verticalCenter: parent.verticalCenter }
                    source: "image://theme/icon-m-add"
                    width: Theme.iconSizeMedium; height: width
                }
                Label {
                    anchors { left: plusIcon.right; leftMargin: Theme.paddingMedium
                              verticalCenter: parent.verticalCenter }
                    text: "New playlist…"
                    color: newItem.highlighted ? Theme.highlightColor : Theme.primaryColor
                }
                onClicked: pageStack.push(newDialog)
            }
        }

        delegate: ListItem {
            id: item
            width: listView.width
            contentHeight: Theme.itemSizeMedium
            Label {
                anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                          right: countLabel.left; rightMargin: Theme.paddingMedium
                          verticalCenter: parent.verticalCenter }
                text: modelData.title
                truncationMode: TruncationMode.Fade
                color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
            }
            Label {
                id: countLabel
                anchors { right: parent.right; rightMargin: Theme.horizontalPageMargin
                          verticalCenter: parent.verticalCenter }
                text: "" + (modelData.count || 0)
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }
            onClicked: page.addTo(modelData.id)
        }

        ViewPlaceholder {
            enabled: page.localPlaylists.length === 0
            text: "No local playlists"
            hintText: "Tap New playlist… above to make one"
        }
        VerticalScrollDecorator { }
    }

    // Make a new playlist and drop the video straight into it.
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
            onAccepted: app.backend.createPlaylist(nameField.text.trim(), function(res) {
                if (res && res.id) {
                    app.backend.addToPlaylist(res.id, page.video || {})
                    pageStack.pop(page)
                }
            })
        }
    }
}
