import QtQuick 2.0
import Sailfish.Silica 1.0

// Saved downloads. Tap to play locally; long-press to delete.
Page {
    id: page
    allowedOrientations: Orientation.All

    Component.onCompleted: app.backend.loadDownloads()

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: app.backend.downloads

        header: PageHeader { title: "Downloads" }

        delegate: ListItem {
            id: item
            width: listView.width

            menu: ContextMenu {
                MenuItem {
                    text: "Delete"
                    onClicked: app.backend.deleteDownload(modelData.id, modelData.kind)
                }
            }

            Image {
                id: kindIcon
                anchors {
                    left: parent.left; leftMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                source: "image://theme/icon-m-play"
                width: Theme.iconSizeMedium; height: width
            }
            Column {
                anchors {
                    left: kindIcon.right; leftMargin: Theme.paddingMedium
                    right: parent.right; rightMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                Label {
                    width: parent.width
                    text: modelData.title
                    truncationMode: TruncationMode.Fade
                    color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
                }
                Label {
                    width: parent.width
                    text: modelData.kind === "audio" ? "Audio" : "Video"
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.secondaryColor
                }
            }

            onClicked: pageStack.push(Qt.resolvedUrl("LocalPlayerPage.qml"),
                { title: modelData.title, path: modelData.path, kind: modelData.kind })
        }

        ViewPlaceholder {
            enabled: app.backend.downloads.length === 0
            text: "No downloads"
            hintText: "Open a video, pull down, and pick Download"
        }
        VerticalScrollDecorator { }
    }
}
