import QtQuick 2.0
import Sailfish.Silica 1.0

// A channel profile: header + recent uploads. `channelRef` may be a channel_id or any
// channel URL — the Python side normalises it. Reached from a video's channel name and
// from the subscriptions list.
Page {
    id: page
    allowedOrientations: Orientation.All

    property string channelRef: ""
    property string channelName: ""
    property string channelId: ""
    property string channelUrl: ""
    property string channelThumb: ""
    property int channelSubs: 0
    property int videoCount: 0
    property bool loading: true
    property string errorText: ""

    // Paging: the uploads list fills in a page at a time as it's scrolled.
    property bool loadingMore: false
    property bool hasMore: true
    property int nextStart: 1
    readonly property int pageSize: 30

    property bool subscribed: {
        var subs = app.backend.subscriptions
        for (var i = 0; i < subs.length; i++)
            if (subs[i].id === page.channelId)
                return true
        return false
    }

    ListModel { id: videosModel }

    // Fetch one page of uploads. start<=1 is the initial load (sets header + clears);
    // later pages just append. Callback-scoped, so a stale page can't grab our results.
    function loadPage(start) {
        if (page.loadingMore)
            return
        if (start > 1)
            page.loadingMore = true
        app.backend.channelVideos(page.channelRef, start, function(res) {
            page.loading = false
            page.loadingMore = false
            if (!res || !res.ok) {
                if (start <= 1) {
                    var err = (res && res.error) ? res.error : "channel failed"
                    // Topic/music channels have no Videos tab at all (yt-dlp errors rather than
                    // returning empty) — jump straight to their playlists/releases instead.
                    if (/videos tab/i.test(err)) {
                        pageStack.replace(Qt.resolvedUrl("ChannelPlaylistsPage.qml"),
                            { channelRef: page.channelRef, channelName: page.channelName })
                        return
                    }
                    page.errorText = err
                }
                return
            }
            if (start <= 1) {
                var ch = res.channel
                if (ch) {
                    page.channelId = ch.id || page.channelId
                    page.channelName = ch.name || page.channelName
                    page.channelUrl = ch.url || page.channelUrl
                    // Keep a good avatar from the caller if this fetch lacks one.
                    if (ch.thumbnail) page.channelThumb = ch.thumbnail
                    page.channelSubs = ch.subscribers || 0
                    page.videoCount = ch.video_count || 0
                }
                videosModel.clear()
            }
            var items = res.items || []
            if (start <= 1 && items.length === 0) {
                // No uploads (music/topic channels, or playlist-only channels) → their playlists.
                pageStack.replace(Qt.resolvedUrl("ChannelPlaylistsPage.qml"),
                    { channelRef: page.channelRef, channelName: page.channelName })
                return
            }
            for (var i = 0; i < items.length; i++)
                videosModel.append(items[i])
            page.hasMore = !!res.has_more
            page.nextStart = start + page.pageSize
        })
    }

    Component.onCompleted: loadPage(1)

    function fmtDur(sec) {
        if (!sec || sec <= 0)
            return ""
        sec = Math.floor(sec)
        var h = Math.floor(sec / 3600)
        var m = Math.floor((sec % 3600) / 60)
        var s = sec % 60
        function p(n) { return (n < 10 ? "0" : "") + n }
        return (h > 0 ? h + ":" + p(m) : "" + m) + ":" + p(s)
    }
    function fmtCount(n) {
        if (!n || n < 0) return "0"
        if (n >= 1000000) return (n / 1000000).toFixed(1).replace(".0", "") + "M"
        if (n >= 1000) return (n / 1000).toFixed(1).replace(".0", "") + "K"
        return "" + n
    }

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: videosModel

        PullDownMenu {
            MenuItem {
                text: "Playlists"
                onClicked: pageStack.push(Qt.resolvedUrl("ChannelPlaylistsPage.qml"),
                    { channelRef: page.channelRef, channelName: page.channelName })
            }
        }

        // Page in more uploads when the list is scrolled to the end.
        onAtYEndChanged: {
            if (atYEnd && page.hasMore && !page.loadingMore && !page.loading
                    && videosModel.count > 0)
                page.loadPage(page.nextStart)
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

        header: Column {
            width: listView.width
            spacing: Theme.paddingLarge

            PageHeader { title: "Channel" }

            // Row 1: profile picture + channel name together.
            Item {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                height: Math.max(avatar.height, nameLabel.height)
                visible: !page.loading && page.errorText.length === 0

                Image {
                    id: avatar
                    anchors { left: parent.left; verticalCenter: parent.verticalCenter }
                    width: page.channelThumb == "" ? 0 : Theme.itemSizeLarge
                    height: width
                    fillMode: Image.PreserveAspectCrop
                    smooth: true
                    asynchronous: true
                    source: page.channelThumb
                }
                Label {
                    id: nameLabel
                    anchors {
                        left: avatar.right
                        leftMargin: page.channelThumb == "" ? 0 : Theme.paddingLarge
                        right: parent.right
                        verticalCenter: parent.verticalCenter
                    }
                    text: page.channelName
                    verticalAlignment: Text.AlignVCenter
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    font.pixelSize: Theme.fontSizeLarge
                    color: Theme.highlightColor
                }
            }

            // Stats line, then the Subscribe action centred below it.
            Column {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                spacing: Theme.paddingMedium
                visible: !page.loading && page.errorText.length === 0

                Label {
                    width: parent.width
                    visible: text.length > 0
                    horizontalAlignment: Text.AlignHCenter
                    text: {
                        var parts = []
                        if (page.channelSubs > 0)
                            parts.push(page.fmtCount(page.channelSubs) + " subscribers")
                        // Only trust a count above the fetch window (a real metadata total,
                        // not the sliced page size).
                        if (page.videoCount > page.pageSize)
                            parts.push(page.fmtCount(page.videoCount) + " videos")
                        return parts.join("  ·  ")
                    }
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.secondaryColor
                }
                Button {
                    anchors.horizontalCenter: parent.horizontalCenter
                    visible: page.channelId.length > 0
                    text: page.subscribed ? "Subscribed" : "Subscribe"
                    onClicked: app.backend.toggleSubscription(
                        page.channelId, page.channelName, page.channelUrl, page.channelThumb)
                }
            }

            Label {
                visible: page.errorText.length > 0
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                color: Theme.errorColor
                font.pixelSize: Theme.fontSizeSmall
                text: page.errorText
            }

            // Breathing room between the header and the first video row.
            Item { width: 1; height: Theme.paddingLarge }
        }

        delegate: ListItem {
            id: delegateItem
            width: listView.width
            property real thumbW: Math.round(width * 0.42)
            property real thumbH: Math.round(thumbW * 9 / 16)
            contentHeight: thumbH + 2 * Theme.paddingSmall

            // Long-press → add to a playlist, or download this video (audio or muxed).
            menu: ContextMenu {
                MenuItem {
                    text: "Add to playlist"
                    onClicked: pageStack.push(Qt.resolvedUrl("AddToPlaylistPage.qml"),
                        { video: { id: model.id, title: model.title, uploader: model.uploader || "",
                                   duration: model.duration || 0, thumbnail: model.thumbnail || "" } })
                }
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
            }

            Rectangle {
                id: vThumb
                anchors {
                    left: parent.left; leftMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                width: delegateItem.thumbW; height: delegateItem.thumbH
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
                    anchors {
                        right: parent.right; bottom: parent.bottom
                        margins: Theme.paddingSmall / 2
                    }
                    radius: 3
                    color: "#C8000000"
                    width: durLabel.width + Theme.paddingSmall
                    height: durLabel.height + Theme.paddingSmall / 2
                    Label {
                        id: durLabel
                        anchors.centerIn: parent
                        text: page.fmtDur(model.duration)
                        color: "white"
                        font.pixelSize: Theme.fontSizeExtraSmall
                    }
                }
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
                    color: delegateItem.highlighted ? Theme.highlightColor : Theme.primaryColor
                    font.pixelSize: Theme.fontSizeSmall
                }
                Label {
                    width: parent.width
                    visible: text.length > 0
                    text: {
                        var parts = []
                        if (model.views > 0) parts.push(page.fmtCount(model.views) + " views")
                        if (model.posted) parts.push(model.posted)
                        return parts.join("  ·  ")
                    }
                    truncationMode: TruncationMode.Fade
                    color: Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                }
            }

            onClicked: pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                { videoId: model.id, title: model.title })
        }

        ViewPlaceholder {
            enabled: !page.loading && videosModel.count === 0 && page.errorText.length === 0
            text: "No videos"
        }

        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading
    }
}
