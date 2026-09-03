import QtQuick 2.0
import Sailfish.Silica 1.0

// Recently-watched videos, newest first. Tap to reopen (resumes from the saved spot). The
// thumbnail carries the same progress bar / WATCHED badge as everywhere else.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool loading: true
    property bool loadingMore: false
    property bool hasMore: true
    property int nextStart: 1
    readonly property int pageSize: 40      // must match the n passed by Backend.watchHistory

    ListModel { id: histModel }

    // Load the first page (newest). A large history is slow to append to a ListModel in one chunk,
    // so the rest pages in as the list is scrolled (loadMore) instead of blocking on the whole lot.
    function reload() {
        page.loading = true
        page.hasMore = true
        page.nextStart = 1
        app.backend.watchHistory(1, function(list) {
            if (!page) return   // the page was popped before this async callback returned
            histModel.clear()
            for (var i = 0; i < list.length; i++)
                histModel.append(list[i])
            page.loading = false
            page.hasMore = list.length >= page.pageSize
            page.nextStart = 1 + page.pageSize
        })
    }

    // Append the next page when the list is scrolled near the end.
    function loadMore() {
        if (page.loadingMore || page.loading || !page.hasMore)
            return
        page.loadingMore = true
        app.backend.watchHistory(page.nextStart, function(list) {
            if (!page) return   // the page was popped before this async callback returned
            page.loadingMore = false
            for (var i = 0; i < list.length; i++)
                histModel.append(list[i])
            page.hasMore = list.length >= page.pageSize
            page.nextStart += page.pageSize
        })
    }
    Component.onCompleted: reload()

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: histModel

        // Page in more history when scrolled to the end.
        onAtYEndChanged: {
            if (atYEnd && page.hasMore && !page.loadingMore && !page.loading
                    && histModel.count > 0)
                page.loadMore()
        }

        footer: Item {
            width: listView.width
            height: (page.loadingMore || page.hasMore) ? Theme.itemSizeLarge : 0
            BusyIndicator {
                anchors.centerIn: parent
                size: BusyIndicatorSize.Medium
                running: page.loadingMore
                visible: running
            }
        }

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
                MenuItem {
                    text: "Video details"
                    onClicked: pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                        { videoId: model.id, title: model.title, infoOnly: true,
                          infoThumbnail: model.thumbnail || "" })
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
