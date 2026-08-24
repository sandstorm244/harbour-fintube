import QtQuick 2.0
import Sailfish.Silica 1.0

// One playlist's videos. Tap a video to play it. (Part 2 will make it auto-continue down the
// list from the tapped point.)
Page {
    id: page
    allowedOrientations: Orientation.All

    property string playlistId: ""
    property string playlistTitle: ""
    property string playlistKind: "local"
    property string ytRef: ""          // set → view an unsaved YouTube playlist by list id/URL
    property bool ytView: ytRef.length > 0
    property bool saved: false
    property bool loading: true

    ListModel { id: itemsModel }

    function fmtDur(sec) {
        if (!sec || sec <= 0) return ""
        sec = Math.floor(sec)
        var h = Math.floor(sec / 3600)
        var m = Math.floor((sec % 3600) / 60)
        var s = sec % 60
        function p(nn) { return (nn < 10 ? "0" : "") + nn }
        return (h > 0 ? h + ":" + p(m) : "" + m) + ":" + p(s)
    }

    function load() {
        page.loading = true
        if (page.ytView) {
            app.backend.youtubePlaylist(page.ytRef, function(res) {
                page.loading = false
                itemsModel.clear()
                if (!res || !res.ok) return
                page.playlistTitle = res.title || page.playlistTitle
                page.playlistKind = "youtube"
                var yits = res.items || []
                for (var j = 0; j < yits.length; j++) itemsModel.append(yits[j])
            })
            return
        }
        app.backend.getPlaylist(page.playlistId, function(pl) {
            page.loading = false
            itemsModel.clear()
            if (!pl) return
            page.playlistTitle = pl.title || page.playlistTitle
            page.playlistKind = pl.kind || "local"
            var its = pl.items || []
            for (var i = 0; i < its.length; i++) itemsModel.append(its[i])
        })
    }
    Component.onCompleted: load()

    // Play the tapped video and seed the autoplay queue so it continues down the list from here.
    function playAt(index) {
        if (index < 0 || index >= itemsModel.count) return
        var arr = []
        for (var i = 0; i < itemsModel.count; i++) {
            var it = itemsModel.get(i)
            arr.push({ id: it.id, title: it.title || "" })
        }
        app.playQueue = arr
        app.playQueueIndex = index
        pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
            { videoId: arr[index].id, title: arr[index].title, fromQueue: true })
    }

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: itemsModel

        PullDownMenu {
            MenuItem {
                visible: page.ytView
                text: page.saved ? "Saved to library ✓" : "Save to library"
                enabled: !page.saved
                onClicked: { app.backend.saveYoutubePlaylist(page.ytRef); page.saved = true }
            }
            MenuItem {
                visible: page.playlistKind === "youtube" && !page.ytView
                text: "Refresh from YouTube"
                onClicked: app.backend.refreshPlaylist(page.playlistId, function() { page.load() })
            }
            MenuItem {
                text: "Play all"
                enabled: itemsModel.count > 0
                onClicked: page.playAt(0)
            }
        }

        header: PageHeader { title: page.playlistTitle }

        delegate: ListItem {
            id: item
            width: listView.width
            property real thumbW: Math.round(width * 0.42)
            property real thumbH: Math.round(thumbW * 9 / 16)
            contentHeight: thumbH + 2 * Theme.paddingSmall

            menu: ContextMenu {
                MenuItem {
                    text: "Download audio"
                    onClicked: {
                        var vid = model.id, t = model.title
                        Remorse.popupAction(page, "Downloading audio",
                            function() { app.backend.download(vid, t, "audio") })
                    }
                }
                MenuItem {
                    text: "Download video"
                    onClicked: {
                        var vid = model.id, t = model.title
                        Remorse.popupAction(page, "Downloading video",
                            function() { app.backend.download(vid, t, "video") })
                    }
                }
                MenuItem {
                    visible: page.playlistKind === "local"
                    text: "Remove from playlist"
                    onClicked: {
                        var vid = model.id
                        Remorse.popupAction(page, "Removing", function() {
                            app.backend.removeFromPlaylist(page.playlistId, vid,
                                function() { page.load() })
                        })
                    }
                }
            }

            Rectangle {
                id: vThumb
                anchors { left: parent.left; leftMargin: Theme.horizontalPageMargin
                          verticalCenter: parent.verticalCenter }
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
                Rectangle {
                    visible: model.duration > 0
                    anchors { right: parent.right; bottom: parent.bottom; margins: Theme.paddingSmall / 2 }
                    radius: 3
                    color: "#C8000000"
                    width: durLabel.width + Theme.paddingSmall
                    height: durLabel.height + Theme.paddingSmall / 2
                    Label { id: durLabel; anchors.centerIn: parent; text: page.fmtDur(model.duration)
                            color: "white"; font.pixelSize: Theme.fontSizeExtraSmall }
                }
            }
            Column {
                anchors { left: vThumb.right; leftMargin: Theme.paddingMedium
                          right: parent.right; rightMargin: Theme.horizontalPageMargin
                          verticalCenter: parent.verticalCenter }
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
                    text: model.uploader || ""
                    truncationMode: TruncationMode.Fade
                    color: Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                }
            }

            onClicked: page.playAt(index)
        }

        ViewPlaceholder {
            enabled: !page.loading && itemsModel.count === 0
            text: "Empty playlist"
            hintText: page.playlistKind === "local"
                      ? "Long-press a video anywhere → Add to playlist" : ""
        }
        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading
    }
}
