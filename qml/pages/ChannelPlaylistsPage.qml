import QtQuick 2.0
import Sailfish.Silica 1.0

// A channel's playlists (from its Playlists tab, or Releases for music channels). Tap one to
// view + play it (opens PlaylistPage in YouTube-view mode).
Page {
    id: page
    allowedOrientations: Orientation.All

    property string channelRef: ""     // channel id or URL
    property string channelName: ""
    property bool loading: true

    ListModel { id: plModel }

    function load() {
        page.loading = true
        app.backend.channelPlaylists(page.channelRef, function(res) {
            page.loading = false
            plModel.clear()
            var items = (res && res.items) ? res.items : []
            for (var i = 0; i < items.length; i++) plModel.append(items[i])
        })
    }
    Component.onCompleted: load()

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: plModel

        header: PageHeader { title: page.channelName || "Playlists" }

        delegate: ListItem {
            id: item
            width: listView.width
            contentHeight: Theme.itemSizeLarge

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
                    source: model.thumbnail || ""
                }
            }
            Column {
                anchors { left: cover.right; leftMargin: Theme.paddingMedium
                          right: parent.right; rightMargin: Theme.horizontalPageMargin
                          verticalCenter: parent.verticalCenter }
                Label {
                    width: parent.width
                    text: model.title
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
                }
                Label {
                    width: parent.width
                    visible: model.count > 0
                    text: (model.count === 1) ? "1 video" : (model.count + " videos")
                    font.pixelSize: Theme.fontSizeExtraSmall
                    color: Theme.secondaryColor
                }
            }

            onClicked: pageStack.push(Qt.resolvedUrl("PlaylistPage.qml"),
                { ytRef: model.yt_id, playlistTitle: model.title })
        }

        ViewPlaceholder {
            enabled: !page.loading && plModel.count === 0
            text: "No playlists"
        }
        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading
    }
}
