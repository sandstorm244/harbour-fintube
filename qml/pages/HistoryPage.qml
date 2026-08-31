import QtQuick 2.0
import Sailfish.Silica 1.0

// Recently-watched videos, newest first. Tap to reopen (resumes from the saved spot). The
// thumbnail carries the same progress bar / WATCHED badge as everywhere else.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool loading: true

    ListModel { id: histModel }

    function reload() {
        page.loading = true
        app.backend.watchHistory(function(list) {
            histModel.clear()
            for (var i = 0; i < list.length; i++)
                histModel.append(list[i])
            page.loading = false
        })
    }
    Component.onCompleted: reload()

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: histModel

        header: PageHeader { title: "History" }

        PullDownMenu {
            MenuItem {
                text: "Clear history"
                onClicked: Remorse.popupAction(page, "Clearing history", function() {
                    app.backend.clearWatchHistory(function() { page.reload() })
                })
            }
        }

        delegate: ListItem {
            id: item
            width: listView.width
            property real thumbW: Math.round(width * 0.42)
            property real thumbH: Math.round(thumbW * 9 / 16)
            contentHeight: thumbH + 2 * Theme.paddingSmall
            onClicked: pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                { videoId: model.id, title: model.title })

            // Long-press → toggle watched (these rows ARE the watch store).
            menu: ContextMenu {
                MenuItem {
                    text: (app.backend.watchMap && app.backend.watchMap[model.id]
                           && app.backend.watchMap[model.id].w) ? "Mark as unwatched" : "Mark as watched"
                    onClicked: {
                        var e = app.backend.watchMap ? app.backend.watchMap[model.id] : undefined
                        app.backend.setWatched(model.id, !(e && e.w), model.title, model.channel || "")
                    }
                }
            }

            Rectangle {
                id: vThumb
                anchors {
                    left: parent.left; leftMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                width: item.thumbW; height: item.thumbH
                radius: Theme.paddingSmall / 2
                color: Theme.rgba(Theme.secondaryColor, 0.2)
                clip: true
                Image {
                    anchors.fill: parent
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    source: model.thumbnail || ""
                }
                WatchOverlay { anchors.fill: parent; videoId: model.id || "" }
            }
            Column {
                anchors {
                    left: vThumb.right; leftMargin: Theme.paddingMedium
                    right: parent.right; rightMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                Label {
                    width: parent.width
                    text: model.title
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
                    font.pixelSize: Theme.fontSizeSmall
                }
                Label {
                    width: parent.width
                    visible: text.length > 0
                    text: model.channel || ""
                    truncationMode: TruncationMode.Fade
                    color: Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                }
            }
        }

        ViewPlaceholder {
            enabled: !page.loading && histModel.count === 0
            text: "No history yet"
            hintText: "Videos you watch show up here."
        }

        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading && histModel.count === 0
    }
}
